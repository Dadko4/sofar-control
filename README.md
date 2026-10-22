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
Kapacitu batérie a horný limit vybíjacieho výkonu nastav cez
`SOFAR_BATTERY_CAPACITY_KWH` (predvolene 5 kWh) a
`SOFAR_MAX_DISCHARGE_POWER_W` (predvolene 2 500 W, povolený rozsah 100–2 500 W).

Rozhranie ponúka Self-use a časovo obmedzené nabíjanie do 95 % s nastaviteľným
výkonom najviac 2 kW. Nabíjací plán začne od aktuálnej minúty a platí do 23:59
aktuálneho dňa. Prepíše existujúce pravidlá Time-of-Use a ponechá aktívne iba
nové pravidlo; po skončení plánu sa režim nemusí sám vrátiť na Self-use, preto
ho skontroluj a prípadne prepnúť späť manuálne.
Dashboard zobrazuje nabíjanie a vybíjanie zvlášť z polí SofarCloud
`chargePower` a `disChargePower`; vybíjací výkon sa ukazuje ako kladná veľkosť,
aj keď API posiela zápornú hodnotu. Zobrazuje tiež kapacitu batérie,
dennú energiu výroby a spotreby, import/export siete a nabíjania/vybíjania
batérie, vrátane jednotiek z odpovede SofarCloud.

Vybíjanie do siete používa kandidátsky Passive režim (kód 3), nastaviteľný výkon
do 2 500 W a cieľ 30 % SOC. Payload používa záporný výkon v poliach
`passiveManualBup` a `passiveManualBlo`, podľa mapovania v SofarCloud webovom
klientovi pre model `HYD 5-20KTL-3PH`. Živá schéma zariadenia neponúka režim 3
v zozname podporovaných režimov, preto je ovládanie predvolene vypnuté.
V tomto projekte bol export do siete overený na zariadení krátkym 500 W testom.
Povoľ ho cez `SOFAR_ENABLE_PASSIVE_DISCHARGE=true` iba pre overený model,
zachovaj dohľad a skontroluj správanie pri aktualizácii režimu či výpadku.

Odhad času a energie vychádza z nastavenej kapacity batérie. Server kontroluje
SOC každých 10 sekúnd a pri dosiahnutí 30 % sa pokúsi prepnúť menič na Self-use.
Odhadovaný čas vybíjania je bezpečnostný záložný limit. Ak server alebo
SofarCloud nie sú dostupné, dohľad nemusí vedieť zastaviť menič. Nechaj
aplikáciu aj spojenie so SofarCloud dostupné počas celého testu a po ňom
skontroluj režim. Schéma neposkytuje potvrdený hardvérový minimálny SOC, takže
softvérové zastavenie nezaručuje, že SOC nikdy neklesne pod 30 %. Skutočný export
batérie do siete bol potvrdený pri krátkom teste; hardvérové limity a nastavenia
DoD/EPS treba potvrdiť v dokumentácii konkrétnej batérie a priamo v meniči.
Dashboard zobrazuje nabíjanie a vybíjanie zvlášť z polí SofarCloud
`chargePower` a `disChargePower`; vybíjací výkon sa ukazuje ako kladná veľkosť,
aj keď API posiela zápornú hodnotu. Čas merania z poľa `dataTime` sa zobrazuje
oddelene od času načítania odpovede, aby bolo viditeľné oneskorenie telemetrie.

Pred každým príkazom sa zobrazí potvrdenie; nabíjanie sa odmietne, ak aktuálne
SOC nie je dostupné alebo je aspoň 95 %. Nabíjací plán sa pri čítaní späť overí
vrátane SOC cieľa, výkonu a časového okna. Pri Passive režime sa spätne overí
aktívny režim a výkonový povel; hranicu 30 % zabezpečuje iba softvérový watchdog.
Live schéma pre tento menič neuvádza
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
