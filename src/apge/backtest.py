from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import Iterable, Sequence

from apge.adaptive_policy import AdaptivePolicyConfig, build_adaptive_grid_decision
from apge.grid_strategy import MarketRegime, OrderProposal, generate_grid_proposals
from apge.market_regime import Candle, RegimeConfig, assess_market_regime


@dataclass(frozen=True)
class BacktestConfig:
    initial_equity: Decimal = Decimal("10000")
    nominal_base_size: Decimal = Decimal("0.001")
    level_count: int = 2
    max_inventory: Decimal = Decimal("0.005")
    tick_size: Decimal = Decimal("0.1")
    step_size: Decimal = Decimal("0.001")
    min_qty: Decimal = Decimal("0.001")
    min_notional: Decimal = Decimal("5")
    synthetic_spread_bps: Decimal = Decimal("2")
    fee_rate: Decimal = Decimal("0.0002")
    conservative_dual_touch: bool = True


@dataclass(frozen=True)
class Fill:
    bar_index: int
    side: str
    price: Decimal
    quantity: Decimal
    fee: Decimal


@dataclass(frozen=True)
class EquityPoint:
    bar_index: int
    close: Decimal
    position: Decimal
    cash: Decimal
    equity: Decimal
    drawdown: Decimal
    regime: MarketRegime


@dataclass(frozen=True)
class BacktestResult:
    initial_equity: Decimal
    final_equity: Decimal
    net_pnl: Decimal
    max_drawdown: Decimal
    max_abs_inventory: Decimal
    total_fees: Decimal
    total_funding: Decimal
    turnover: Decimal
    fill_count: int
    bars_processed: int
    fills: tuple[Fill, ...]
    equity_curve: tuple[EquityPoint, ...]


def _finite(value: Decimal) -> bool:
    return isinstance(value, Decimal) and not value.is_nan() and not value.is_infinite()


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _validate_config(config: BacktestConfig) -> None:
    decimal_values = (
        config.initial_equity,
        config.nominal_base_size,
        config.max_inventory,
        config.tick_size,
        config.step_size,
        config.min_qty,
        config.min_notional,
        config.synthetic_spread_bps,
        config.fee_rate,
    )
    if any(not _finite(value) for value in decimal_values):
        raise ValueError("backtest configuration must contain finite Decimals")
    if config.initial_equity <= 0:
        raise ValueError("initial equity must be positive")
    if config.nominal_base_size <= 0 or config.max_inventory <= 0:
        raise ValueError("base size and max inventory must be positive")
    if config.tick_size <= 0 or config.step_size <= 0:
        raise ValueError("tick/step size must be positive")
    if config.min_qty < 0 or config.min_notional < 0 or config.fee_rate < 0:
        raise ValueError("minimums and fee rate cannot be negative")
    if config.synthetic_spread_bps <= 0:
        raise ValueError("synthetic spread must be positive")
    if config.level_count <= 0:
        raise ValueError("level count must be positive")


def _synthetic_bba(reference: Decimal, spread_bps: Decimal) -> tuple[Decimal, Decimal]:
    half = reference * spread_bps / Decimal("20000")
    bid = reference - half
    ask = reference + half
    if bid <= 0 or bid >= ask:
        raise ValueError("synthetic BBA is invalid")
    return bid, ask


def _filter_proposals(proposals: Iterable[OrderProposal], config: BacktestConfig) -> list[OrderProposal]:
    valid = []
    for proposal in proposals:
        if proposal.quantity < config.min_qty:
            continue
        if proposal.price * proposal.quantity < config.min_notional:
            continue
        valid.append(proposal)
    return valid


def _touched(proposal: OrderProposal, bar: Candle) -> bool:
    if proposal.side == "BUY":
        return bar.low <= proposal.price
    if proposal.side == "SELL":
        return bar.high >= proposal.price
    raise ValueError(f"unknown side {proposal.side}")


def _select_fills(
    proposals: Sequence[OrderProposal],
    bar: Candle,
    current_position: Decimal,
    conservative_dual_touch: bool,
) -> list[OrderProposal]:
    touched = [proposal for proposal in proposals if _touched(proposal, bar)]
    if not conservative_dual_touch:
        return touched

    buys = [proposal for proposal in touched if proposal.side == "BUY"]
    sells = [proposal for proposal in touched if proposal.side == "SELL"]
    if not buys or not sells:
        return touched

    # OHLC bars do not reveal intrabar path. When both sides were touched, do not
    # grant a synthetic round-trip profit. Keep only the side that is more adverse
    # to the bar's close direction / current inventory. This is intentionally
    # conservative and deterministic.
    if current_position > 0:
        selected_side = "BUY"  # increases long inventory
    elif current_position < 0:
        selected_side = "SELL"  # increases short inventory
    elif bar.close > bar.open:
        selected_side = "SELL"  # short into an up-close bar
    else:
        selected_side = "BUY"  # long into a down/doji close bar
    return [proposal for proposal in touched if proposal.side == selected_side]


def _apply_fill(
    *,
    cash: Decimal,
    position: Decimal,
    proposal: OrderProposal,
    fee_rate: Decimal,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    notional = proposal.price * proposal.quantity
    fee = abs(notional) * fee_rate
    if proposal.side == "BUY":
        cash -= notional
        position += proposal.quantity
    elif proposal.side == "SELL":
        cash += notional
        position -= proposal.quantity
    else:
        raise ValueError(f"unknown side {proposal.side}")
    cash -= fee
    return cash, position, fee, abs(notional)


def run_backtest(
    candles: Sequence[Candle],
    *,
    funding_rates: Sequence[Decimal] | None = None,
    config: BacktestConfig = BacktestConfig(),
    regime_config: RegimeConfig = RegimeConfig(),
    policy_config: AdaptivePolicyConfig = AdaptivePolicyConfig(),
) -> BacktestResult:
    """Run a deterministic candle-level adaptive-grid simulation.

    The engine intentionally models only linear PnL, fees, funding and inventory.
    It does not model liquidation/margin mechanics and therefore must not be used
    as evidence that leveraged live trading is safe. Orders are repriced at each
    bar boundary. Only information from already-closed bars is used to construct
    the next bar's grid, avoiding look-ahead in regime classification.
    """
    _validate_config(config)
    if len(candles) < regime_config.lookback + 1:
        raise ValueError("insufficient candles for backtest")
    for candle in candles:
        # Reuse the classifier's strict validation without exporting internals by
        # validating through a one-window call below before a candle is traded.
        if not all(_finite(v) and v > 0 for v in (candle.open, candle.high, candle.low, candle.close)):
            raise ValueError("candles must contain finite positive Decimals")
        if candle.low > candle.high or candle.open < candle.low or candle.open > candle.high or candle.close < candle.low or candle.close > candle.high:
            raise ValueError("invalid candle OHLC geometry")

    if funding_rates is None:
        funding_rates = [Decimal("0")] * len(candles)
    if len(funding_rates) != len(candles):
        raise ValueError("funding_rates length must match candles length")
    if any(not _finite(rate) for rate in funding_rates):
        raise ValueError("funding rates must be finite Decimals")

    cash = config.initial_equity
    position = Decimal("0")
    peak_equity = config.initial_equity
    max_drawdown = Decimal("0")
    max_abs_inventory = Decimal("0")
    total_fees = Decimal("0")
    total_funding = Decimal("0")
    turnover = Decimal("0")
    fills: list[Fill] = []
    equity_curve: list[EquityPoint] = []

    first_execution_index = regime_config.lookback
    for index in range(first_execution_index, len(candles)):
        execution_bar = candles[index]
        history = candles[index - regime_config.lookback:index]
        assessment = assess_market_regime(history, regime_config)
        reference = history[-1].close
        best_bid, best_ask = _synthetic_bba(reference, config.synthetic_spread_bps)

        decision = build_adaptive_grid_decision(
            best_bid=best_bid,
            best_ask=best_ask,
            current_inventory=position,
            max_inventory=config.max_inventory,
            nominal_base_size=config.nominal_base_size,
            step_size=config.step_size,
            assessment=assessment,
            funding_rate=funding_rates[index],
            config=policy_config,
        )

        proposals = generate_grid_proposals(
            best_bid=best_bid,
            best_ask=best_ask,
            current_inventory=position,
            grid_spacing=decision.grid_spacing,
            base_size=decision.base_size,
            level_count=config.level_count,
            max_inventory=config.max_inventory,
            tick_size=config.tick_size,
            step_size=config.step_size,
            system_state=__import__("apge.simulator", fromlist=["SystemState"]).SystemState.OPERATIONAL,
            market_regime=decision.regime,
            is_stale_data=False,
            inventory_target=decision.target_inventory,
            inventory_skew_strength=decision.inventory_skew_strength,
        )
        proposals = _filter_proposals(proposals, config)
        selected = _select_fills(
            proposals,
            execution_bar,
            position,
            config.conservative_dual_touch,
        )

        # Process closer-to-market orders first on each side and reject any fill
        # that would violate the same hard inventory envelope used by GridStrategy.
        selected.sort(key=lambda p: p.price, reverse=True if selected and selected[0].side == "BUY" else False)
        for proposal in selected:
            signed_qty = proposal.quantity if proposal.side == "BUY" else -proposal.quantity
            candidate_position = position + signed_qty
            if abs(candidate_position) > config.max_inventory:
                continue
            cash, position, fee, fill_turnover = _apply_fill(
                cash=cash,
                position=position,
                proposal=proposal,
                fee_rate=config.fee_rate,
            )
            total_fees += fee
            turnover += fill_turnover
            fills.append(Fill(index, proposal.side, proposal.price, proposal.quantity, fee))
            max_abs_inventory = max(max_abs_inventory, abs(position))

        # Linear perpetual funding: positive rate means longs pay and shorts receive.
        funding_payment = position * execution_bar.close * funding_rates[index]
        cash -= funding_payment
        total_funding += funding_payment

        equity = cash + position * execution_bar.close
        peak_equity = max(peak_equity, equity)
        drawdown = peak_equity - equity
        max_drawdown = max(max_drawdown, drawdown)
        equity_curve.append(
            EquityPoint(
                bar_index=index,
                close=execution_bar.close,
                position=position,
                cash=cash,
                equity=equity,
                drawdown=drawdown,
                regime=assessment.regime,
            )
        )

    final_equity = equity_curve[-1].equity if equity_curve else config.initial_equity
    return BacktestResult(
        initial_equity=config.initial_equity,
        final_equity=final_equity,
        net_pnl=final_equity - config.initial_equity,
        max_drawdown=max_drawdown,
        max_abs_inventory=max_abs_inventory,
        total_fees=total_fees,
        total_funding=total_funding,
        turnover=turnover,
        fill_count=len(fills),
        bars_processed=len(equity_curve),
        fills=tuple(fills),
        equity_curve=tuple(equity_curve),
    )
