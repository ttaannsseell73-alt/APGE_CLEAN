# APGE V1 Reference Review

*Not: Tüm teknik bulgular statik kod analizi ile incelenmiş olup, testler yerel ortamda **çalıştırılmamıştır**.*

## İncelenen Altyapılar

1. **Hummingbot**
   - **Repo:** https://github.com/hummingbot/hummingbot
   - **Commit SHA:** `2bfaccc48dd49e71a5b6d9b3011808e127dd00cd`
   - **Lisans:** [Apache-2.0](https://github.com/hummingbot/hummingbot/blob/2bfaccc48dd49e71a5b6d9b3011808e127dd00cd/LICENSE)

2. **Freqtrade**
   - **Repo:** https://github.com/freqtrade/freqtrade
   - **Commit SHA:** `243ddaef420f64ae3978ddacd6ffff706e8fb0d7`
   - **Lisans:** [GPL-3.0](https://github.com/freqtrade/freqtrade/blob/243ddaef420f64ae3978ddacd6ffff706e8fb0d7/LICENSE)

## Doğrulanan Teknik Bulgular

### 1. Emir Gönderiminde Timeout/UNKNOWN ve Kayıp Emir (Lost Order) Takibi

**Kaynakta Doğrudan Gözlenen Davranış:**
- **Hummingbot:** [`client_order_tracker.py`](https://github.com/hummingbot/hummingbot/blob/2bfaccc48dd49e71a5b6d9b3011808e127dd00cd/hummingbot/connector/client_order_tracker.py#L254-L266) içindeki `process_order_not_found` metodunda, bulunamayan emirler için bir sayaç tutulur. Limit aşılırsa emir `FAILED` durumuna çekilip `_lost_orders` sözlüğüne (dictionary) taşınır. Emrin takibi tamamen bırakılmaz; [`_process_order_update`](https://github.com/hummingbot/hummingbot/blob/2bfaccc48dd49e71a5b6d9b3011808e127dd00cd/hummingbot/connector/client_order_tracker.py#L295-L302) metodu üzerinden asenkron ağdan sonradan gelen güncellemeler `fetch_lost_order` ile işlenebilir.
  - **Test Kanıtı:** [`test_process_order_not_found_exceeded_limit`](https://github.com/hummingbot/hummingbot/blob/2bfaccc48dd49e71a5b6d9b3011808e127dd00cd/test/hummingbot/connector/test_client_order_tracker.py#L788) test fonksiyonu, limit aşıldığında emrin `active_orders` listesinden çıkarılıp `FAILED` olarak işaretlendiğini doğrular.

- **Freqtrade:** [`freqtradebot.py`](https://github.com/freqtrade/freqtrade/blob/243ddaef420f64ae3978ddacd6ffff706e8fb0d7/freqtrade/freqtradebot.py#L1628-L1660) içindeki `manage_open_orders` metodunda, `exchange.fetch_order()` başarısız olduğunda `ExchangeError` catch bloğuna düşer, `continue` (Satır: 1645) ile döngünün o iterasyonu atlanır. Emir veritabanında "open" kalmaya devam eder.
  - **Test Kanıtı:** [`test_freqtradebot.py`](https://github.com/freqtrade/freqtrade/blob/243ddaef420f64ae3978ddacd6ffff706e8fb0d7/tests/freqtradebot/test_freqtradebot.py) dosya konumu bilinmektedir; ancak spesifik test fonksiyonu ve doğruladığı assertion incelenen kapsamda doğrulanmamıştır (test kanıtı doğrulanmadı).

**İncelemenin Sınırı / Doğrulanamayan Nokta:**
- Bir API sorgusunda emrin bulunamaması veya döngünün bir iterasyonu atlaması tek başına çözülemeyen bütünlük ihlali oluşturmaz. Uzun süreli veya ardışık hataların hangi eşikte mutlak kilit (`HALTED`) üreteceği, incelenen repolarda belirlenmiş bir güvenlik kuralı olarak doğrulanamamıştır.

**APGE İçin Öneri:**
- Bağlantı koptuğunda sistem `CONNECTION_LOST` durumuna geçmeli, `UNKNOWN` (durumu belirsiz) emir kayıtları bu aşamada kesinlikle korunmalıdır. Bağlantı geri geldiğinde durum `RECONCILING` olmalı ve borsa ile mutabakat aranmalıdır. Çözülemeyen bütünlük ihlali halinde `HALTED` durumuna geçme eşikleri (kaç başarısız deneme veya ne kadarlık miktar sapması olacağı) açık bir karar olarak belirlenmelidir. `SHOCK` (piyasa rejimi) ve `DATA_STALL` (veri sağlığı durumu) bağlantı kopukluğundan bağımsız olarak değerlendirilmelidir.

### 2. Kısmi Gerçekleşme ve İptal-Gerçekleşme Yarışları (Cancel-Fill Race Condition)

**Kaynakta Doğrudan Gözlenen Davranış:**
- **Hummingbot:** [`in_flight_order.py`](https://github.com/hummingbot/hummingbot/blob/2bfaccc48dd49e71a5b6d9b3011808e127dd00cd/hummingbot/core/data_type/in_flight_order.py#L210) `update_with_trade_update` metodu ile gerçekleşen hacimler (fills) biriktirilir.
  - **Test Kanıtı:** Kısmi dolum eşzamanlılık testleri incelenen kapsamda doğrulanmadı.
- **Freqtrade:** Açık siparişler `fetch_order` üzerinden dönen miktar (filled) bilgisiyle güncellenir. İptal talebi API'ye iletildikten sonra (`handle_cancel_order`), dönen kısmi gerçekleşmelerin senkronizasyonu yönetilir.
  - **Test Kanıtı:** İptal ve dolum mesajlarının eşzamanlı yarıştığı (cancel-fill race) ağ kopukluğu senaryosu testi incelenen kapsamda doğrulanmadı.

**İncelemenin Sınırı / Doğrulanamayan Nokta:**
- Ağ kopması anında borsadan dönen asenkron "kısmi dolum" ve "iptal onayı" mesajlarının aynı milisaniyede gelmesi veya sırasının bozulması durumunda tutarlılığın nasıl sağlandığı statik analizle kanıtlanmamıştır.

**APGE İçin Öneri:**
- Kısmi gerçekleşmede, sadece gerçekleşen miktar pozisyon riskine aktarılmalı; kalan miktarın riski, emrin iptal edildiği teyit edilene kadar "rezervasyon" altında tutulmalıdır.

### 3. Bağlantı Sonrası Mutabakat (Startup Reconciliation)

**Kaynakta Doğrudan Gözlenen Davranış:**
- **Freqtrade:** Başlangıçta [`startup_update_open_orders()`](https://github.com/freqtrade/freqtrade/blob/243ddaef420f64ae3978ddacd6ffff706e8fb0d7/freqtrade/freqtradebot.py#L160) çağrılır, veritabanındaki açık emirler ile borsa durumu karşılaştırılır.
  - **Test Kanıtı:** Test kanıtı doğrulanmadı.

**İncelemenin Sınırı / Doğrulanamayan Nokta:**
- API limitlerinin mutabakat esnasında dolması durumunda sistemin tepkisi doğrulanmamıştır.

**APGE İçin Öneri:**
- Yetim emirlerde otomatik uyarlama yerine, çözülebilir farklılıklar `RECONCILING`, çözülemeyenler (bütünlük ihlali eşikleri aşıldığında) `HALTED` olarak işlemelidir.

## Eksik Kalan ve Doğrulanamayan İncelemeler

### 1. Atomik Risk Rezervasyonu ve Risk Onayının Gönderim Anında Geçersizleşmesi
- **İnceleme Durumu:** İncelenen her iki kaynak kod repasında (Hummingbot ve Freqtrade), risk motorundan alınan onayın, ağ gecikmesi nedeniyle gönderim anında zaman aşımına uğraması veya atomik (önceden ayrılmış) bir rezervasyon blokuyla emir eşleştirmesi yapıldığına dair kanıt **incelenen kapsamda doğrulanamadı**. Bu yüzden risk izolasyonu mevcut sistemlere bakılarak kıyaslanamadı.

## APGE V1 İçin Sonuç: "Mevcut Tasarımda Koru / Düzelt / Açık Bırak" Tablosu

| Senaryo / Bileşen | APGE V1 Kararı | Karar: Koru / Düzelt / Açık Bırak |
| :--- | :--- | :--- |
| **Bağlantı Kaybı ve Mutabakat** | Bağlantı koptuğunda `CONNECTION_LOST` durumuna geçilir ve `UNKNOWN` emir kayıtları bu süreçte korunur. Bağlantı geri gelince `RECONCILING` süreci başlar. | **Koru** |
| **HALTED Yükseltme Ölçütleri** | Bir sorgu hatasının tek başına sistemi kilitlememesi, `HALTED` için çözülemeyen bütünlük ihlali eşiklerinin (retry/timeout vb.) belirlenmesi gerekir. | **Açık Bırak** (Eşik kuralları henüz belirlenmedi) |
| **Piyasa ve Veri Durumları** | `SHOCK` piyasa rejimi, `DATA_STALL` veri sağlığı durumudur. Bağlantı kopukluğu (`CONNECTION_LOST`) ile eşitlenmemelidir. | **Koru** |
| **Kısmi Gerçekleşme ve Risk Aktarımı** | Yalnız gerçekleşen miktar pozisyon riskine aktarılır; kalan miktar, iptal onayı kesinleşene kadar "rezerv" olarak tutulur. | **Koru** |
| **İptal ve Reduce-Only Ayrımı** | İptal, bekleyen emri geri çeker; reduce-only pozisyon riskini düşürür. İkisi ayrı eylemlerdir ve reduce-only emri, güncel pozisyon doğrulamasını gerektirir. | **Koru** |
| **Atomik Risk Rezervasyonu / Onay Geçersizleşmesi** | Emrin API'ye ulaştığı anda geçerliliğini yitiren risk onayları ve atomik rezervasyon izolasyonu. | **Açık Bırak** (Kanıt bulunamadı, incelenen kapsamda doğrulanamadı) |
| **Eşzamanlı Rezervasyon Testi** | Eşzamanlı race condition senaryolarının asenkron API gecikmelerine karşı test edilmesi. | **Açık Bırak** (Statik analizle kanıtlanamadı, canlı/mock testlerle kanıtlanması gerekir) |
| **Tasarım Belgesinin Yeterliliği** | Tasarım belgelerinin (Risk Engine Contract) tek başına uygulamanın güvenli olduğunu kanıtlamaması; uygulama sırasında sistem testleri gerekmesi. | **Koru** |
