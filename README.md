# Sofar Opti

Lokálna webová appka na čítanie aktuálnych hodnôt a bezpečné overenie
podporovaných režimov cez SofarCloud.
Prihlasovacie meno a heslo sa čítajú výhradne z premenných prostredia.

## Spustenie vo Windows

Vytvor si `.env` súbor v koreňovom priečinku (vedľa `run.py`) a vlož doň
nastavenia podľa `.env.example`:

```dotenv
SOFAR_USERNAME=tvoj prihlasovací e-mail
SOFAR_PASSWORD=tvoje heslo
SOFAR_PLANT_ID=ID elektrárne
SOFAR_INVERTER_MODEL=HYD 5KTL-3PH
SOFAR_COMMAND_MODEL=HYD 5-20KTL-3PH
SOFAR_INVERTER_SN=SP2ES105N7E171
```

Nainštaluj závislosti (`python -m pip install -r requirements.txt`) a spusti
`python run.py`, potom otvor
[http://127.0.0.1:8765](http://127.0.0.1:8765).
Server počúva iba na tomto počítači. `.env` je v `.gitignore`; neposielaj ho
ani nezdieľaj jeho obsah.
Ak bolo heslo niekedy vložené do zdrojového kódu alebo zdieľanej konverzácie,
najprv ho zmeň v SofarCloud.

Ak treba zmeniť port, nastav `SOFAR_WEB_PORT`. Región API možno zmeniť cez
`SOFAR_BASE_URL`, predvolene sa používa `https://eu.sofarcloud.com`.
Časové plány používajú `SOFAR_TIMEZONE` (predvolene `Europe/Bratislava`).

Rozhranie ponúka Self-use a časovo obmedzené nabíjanie do 95 % s nastaviteľným
výkonom najviac 2 kW. Nabíjací plán začne od aktuálnej minúty a platí do 23:59
aktuálneho dňa. Prepíše existujúce pravidlá Time-of-Use a ponechá aktívne iba
nové pravidlo; po skončení plánu sa režim nemusí sám vrátiť na Self-use, preto
ho skontroluj a prípadne prepnúť späť manuálne. Vybíjanie nie je sprístupnené.

Pred každým príkazom sa zobrazí potvrdenie; nabíjanie sa odmietne, ak aktuálne
SOC nie je dostupné alebo je aspoň 95 %. Plán sa pri čítaní späť overí vrátane
SOC cieľa, výkonu a časového okna. Live schéma pre tento menič neuvádza
samostatný prepínač zdroja zo siete ani pravidlo pre prebytok z FVE; pri prvom
teste preto na zariadení over, že menič skutočne nabíja zo siete a posiela
prebytok z FVE do batérie.

## Docker nasadenie

Na serveri nainštaluj Docker Engine a Docker Compose plugin, naklonuj repozitár
a v koreňovom priečinku vytvor `.env` z `.env.example`. Doplň SofarCloud
prihlasovacie údaje, plant ID a jedinečné prihlasovacie meno aj heslo pre web:

```sh
cp .env.example .env
docker compose up -d --build
docker compose logs -f
```

Compose štandardne sprístupní port iba na `127.0.0.1:8765`; aplikácia vyžaduje
Basic prihlasovanie, preto nastav obe premenné `SOFAR_WEB_USERNAME` a
`SOFAR_WEB_PASSWORD`. Nepristupuj k rozhraniu cez nezabezpečené verejné HTTP.
Pre prístup z LAN nastav `SOFAR_BIND_ADDRESS` na IP servera a pridaj presný
origin do `SOFAR_ALLOWED_ORIGINS` (napr.
`http://192.168.1.20:8765`). Odporúčané je sprístupnenie cez reverzný proxy
s HTTPS a jeho autentifikáciou. Port aplikácie nepresmeruj priamo do internetu.

Pri aktualizácii stiahni zmeny a spusti `docker compose up -d --build`.
Kontajner beží ako neprivilegovaný používateľ, s read-only koreňovým
súborovým systémom; `.env` sa kopíruje do prostredia kontajnera, nie do Docker
obrazu. Pred pushnutím na GitHub skontroluj, že `.env` nebol pridaný do gitu.

## Overenie

Lokálne testy, ktoré nepoužívajú skutočné prihlasovacie údaje ani cloud API:

```powershell
python -m unittest -v
```

SofarCloud API nie je oficiálne zdokumentované. Pri zlyhaní stránka zobrazí
fázu (`login`, `station detail`, `command schema`, `mode read`, `mode control`
alebo `mode verification`), API/HTTP kód, typ obsahu a formát odpovede. Tieto
údaje môžeš zdieľať na diagnostiku, nie však prihlasovacie údaje ani access token.
