# XT211 HAN – Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
![Maintained](https://img.shields.io/maintenance/yes/2026)

> **Čtení dat z elektroměru Sagemcom XT211 (ČEZ Distribuce) přes RS485-to-Ethernet adaptér – bez ESP32.**

Tato integrace nahrazuje ESPHome řešení s ESP32 + RS485→TTL převodníkem. Místo toho používá průmyslový RS485-to-Ethernet adaptér (doporučen **PUSR USR-DR134**), který posílá syrová RS485 data přes TCP přímo do Home Assistantu.

---

## Jak to funguje

```
XT211 / WM-RelayBox
   └── RJ12 HAN port (RS485, 9600 baud)
          └── USR-DR134 (RS485 → Ethernet)
                 └── TCP socket (LAN)
                        └── Home Assistant (tato integrace)
```

Elektroměr posílá DLMS/COSEM PUSH zprávy každých **60 sekund**. Integrace udržuje persistentní TCP spojení k adaptéru a dekóduje příchozí HDLC rámce.

---

## Požadavky

- Home Assistant 2024.1+
- RS485-to-Ethernet adaptér s TCP server módem:
  - **PUSR USR-DR134** (doporučeno) – RS485, DIN rail, 5–24V
  - Nebo jiný kompatibilní adaptér (USR-TCP232-410S, Waveshare, apod.)

---

## Instalace přes HACS

1. Otevři HACS → **Integrace** → tři tečky vpravo nahoře → **Vlastní repozitáře**
2. Přidej URL tohoto repozitáře, kategorie: **Integration**  (https://github.com/nero150/CEZ_rele_box)
3. Najdi „XT211 HAN" a nainstaluj
4. Restartuj Home Assistant
5. **Nastavení → Zařízení a služby → Přidat integraci → XT211 HAN**

---

## Nastavení adaptéru USR-DR134

Nastavení přes webové rozhraní adaptéru (výchozí IP `192.168.0.7`):

| Parametr | Hodnota |
|----------|---------|
| Work Mode | **TCP Server** |
| Local Port | `8899` (nebo libovolný) |
| Baud Rate | `9600` |
| Data Bits | `8` |
| Stop Bits | `1` |
| Parity | `None` |
| Flow Control | `None` |

> ⚠️ Použij model **USR-DR134** (RS485), ne DR132 (RS232)!

---

## Zapojení

```
WM-RelayBox HAN port (RJ12):
  Pin 3 (Data A+)  →  USR-DR134 terminal A+
  Pin 4 (Data B-)  →  USR-DR134 terminal B-
  Pin 6 (GND)      →  USR-DR134 GND (volitelné)
```

Napájení USR-DR134: 5–24V DC (např. z USB adaptéru přes step-up, nebo 12V zdroj).

---

## Dostupné senzory

| Název | OBIS kód | Jednotka |
|-------|----------|----------|
| Active Power Consumption | `1-0:1.7.0.255` | W |
| Active Power Delivery | `1-0:2.7.0.255` | W |
| Active Power L1 | `1-0:21.7.0.255` | W |
| Active Power L2 | `1-0:41.7.0.255` | W |
| Active Power L3 | `1-0:61.7.0.255` | W |
| Energy Consumed | `1-0:1.8.0.255` | kWh |
| Energy Consumed T1 | `1-0:1.8.1.255` | kWh |
| Energy Consumed T2 | `1-0:1.8.2.255` | kWh |
| Energy Delivered | `1-0:2.8.0.255` | kWh |
| Serial Number | `0-0:96.1.1.255` | – |
| Current Tariff | `0-0:96.14.0.255` | – |
| Disconnector Status | `0-0:96.3.10.255` | – |

---

## Ladění (debug)

Přidej do `configuration.yaml`:

```yaml
logger:
  default: warning
  logs:
    custom_components.xt211_han: debug
```

V logu uvidíš surová hex data každého HDLC rámce a dekódované OBIS hodnoty.

---

## Struktura repozitáře

```
custom_components/xt211_han/
├── __init__.py          # Inicializace integrace
├── manifest.json        # Metadata pro HA / HACS
├── const.py             # Konstanty
├── config_flow.py       # UI průvodce nastavením
├── coordinator.py       # TCP listener + DataUpdateCoordinator
├── sensor.py            # Senzorová platforma
├── dlms_parser.py       # HDLC / DLMS / COSEM parser
├── strings.json         # Texty UI
└── translations/
    ├── cs.json          # Čeština
    └── en.json          # Angličtina
```

---

## Poděkování / Credits

- [Tomer27cz/xt211](https://github.com/Tomer27cz/xt211) – původní ESPHome komponenta a dokumentace protokolu
- ČEZ Distribuce – dokumentace OBIS kódů a RS485 rozhraní

---

## Licence

MIT
