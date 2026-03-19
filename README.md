# XT211 HAN – Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
![Maintained](https://img.shields.io/maintenance/yes/2026)

> Čtení dat z elektroměru Sagemcom XT211 / Relay box (ČEZ Distribuce) přes RS485-to-Ethernet převodník do Home Assistantu.

Tahle integrace čte push data z HAN / RS485 rozhraní elektroměru přes TCP server na převodníku.

## Jak to funguje

```text
XT211 / Relay box
   └── RJ12 HAN port (RS485, 9600 Bd)
          └── RS485 → Ethernet převodník
                 └── TCP server na LAN
                        └── Home Assistant
```

- Elektroměr posílá jednosměrná DLMS/COSEM data z elektroměru k zákazníkovi rychlostí 9600 Bd a podle dokumentace ČEZ se push zprávy předávají 1× za 60 s.
- Rozhraní je vyvedené na konektoru RJ12, kde je Data A na pinu 3, Data B na pinu 4 a GND na pinu 6.
- Dokumentace také uvádí sadu OBIS kódů pro HAN rozhraní.

## Ověřený hardware

- PUSR USR-USR-DR134
- Předpoklad je, že bude fungovat každý RS485-TCP převodník

## Instalace přes HACS

1. Otevři HACS → **Integrace** → **Vlastní repozitáře**.
2. Přidej URL tohoto repozitáře jako typ **Integration**.
3. Nainstaluj integraci **XT211 HAN**.
4. Restartuj Home Assistant.
5. V **Nastavení → Zařízení a služby** přidej integraci **XT211 HAN**.

## Debug logování

Do `configuration.yaml`:

```yaml
logger:
  default: warning
  logs:
    custom_components.xt211_han: debug
```

## Podklady v repozitáři

- `docs/pdfs/cez_rs485_han_interface.pdf`
- `docs/pdfs/cez_obis_codes_han_2025-02-01.pdf`

## Licence

MIT
