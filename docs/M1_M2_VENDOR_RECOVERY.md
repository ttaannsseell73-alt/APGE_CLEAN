# M1 and M2 Vendor Recovery Guide

## Overview
During the investigation of `experiments/APGE_Hybrid/m1_simulation.py` and `experiments/APGE_Hybrid/m2_simulation.py`, it was discovered that the tests are blocked because the `passivbot` dependency is missing from the vendor directory.

Faithful recovery of these simulation paths requires the original `passivbot` source and the `passivbot_rust` compilation artifact, which provide the deterministic planner behavior required by the APGE architecture.

## Missing Dependencies and Artifacts

1. **Vendor Repository:**
   - `experiments/APGE_Hybrid/vendor/passivbot/` directory is missing.
2. **Missing Python Modules & Symbols:**
   - `vendor.passivbot.tests.test_orchestrator_integration`: Needs to provide `make_input` and `make_symbol`.
3. **Missing Build Artifacts:**
   - `passivbot_rust`: A compiled Rust extension module for Python that provides the `compute_ideal_orders_json` function (`pbr.compute_ideal_orders_json`).

## Minimum Migration Procedure

To restore the M1 and M2 validation paths without fabricating the Passivbot planner's logic, the following steps must be completed:

1. **Restore the Passivbot Source:**
   - Clone or copy the original Passivbot repository into the `experiments/APGE_Hybrid/vendor/passivbot` directory.
   - Ensure the specific commit used in APGE (as printed in the M2 simulation) is checked out.

2. **Recompile the Rust Extension:**
   - Navigate to the Rust project directory within the vendored Passivbot source (or wherever `passivbot_rust` is located).
   - Use the appropriate build tool (e.g., `maturin develop --release` or `cargo build`) to compile the `passivbot_rust` module for the active Python environment.
   - Verify that `import passivbot_rust as pbr` succeeds in Python.

3. **Verify the Python Mocks/Helpers:**
   - Ensure that `vendor/passivbot/tests/test_orchestrator_integration.py` exists and exports the functions `make_input` and `make_symbol`.
   - Ensure that `tests/test_orchestrator_integration.py` is in the `sys.path` (handled by the script, but requires the file to be present).

4. **Run the Simulations:**
   - Execute `python3 experiments/APGE_Hybrid/m1_simulation.py`.
   - Execute `python3 experiments/APGE_Hybrid/m2_simulation.py`.
   - Both should now successfully import Nautilus Trader and the Passivbot dependencies, evaluate the proposals via the risk engine, and assert the expected positional outcomes.
