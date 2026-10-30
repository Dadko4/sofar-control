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
ho skontroluj a prípadne prepnúť späť manuálne. Ak menič v režime Self-use
nevráti uložené pravidlá Time-of-Use, aplikácia použije štandardnú
štvorpravidlovú štruktúru SofarCloud; mimo Self-use sa pri chýbajúcich
pravidlách nabíjanie odmietne, aby neprepísalo neznámy plán.

### Plánovanie podľa tarify Magna 4T

Sekcia „Celodenný plán podľa počasia a tarify 4T“ používa používateľom
nastaviteľnú obec (predvolene Jacovce), výkon panelov (7 kWp), letnú a zimnú
spotrebu (6 a 25 kWh/deň), prepínač energopomoci (predvolene zapnutý) a
plánovací horizont 12, 24 alebo 36 hodín (predvolene 24).
Nastavenia sa ukladajú iba v danom prehliadači. Open-Meteo geokóduje obec na
Slovensku a poskytuje hodinové globálne žiarenie; služba nevyžaduje API kľúč.

Horizont začína od najbližšej štvrthodiny a končí po zvolenom počte
skutočných hodín; nejde o pevný čas 06:00 ani o kalendárny deň. Optimalizátor
(`planner.py`) počíta v 15-minútových intervaloch dynamickým programovaním nad
stavom batérie a vyberá akcie: Self-use alebo nabíjanie zo siete pri 0,5–2 kW.
Výsledkom sú nabíjacie okná s výkonom v odporúčanom čase, cieľovým SOC a
hodinový rozpis.

Ceny sú celkové ceny odberu vrátane DPH a distribučných poplatkov (`tariff.py`):
noc 22–06 (0,117 €/kWh s energopomocou, 0,156 bez nej), ráno/večer špička
06–09 a 19–22 (0,222 / 0,261), dopoludnie 09–13 (0,129 / 0,167) a popoludnie
13–19 (0,167 / 0,206). Cez víkend sa 12 % zľava uplatní iba na silovú elektrinu
(odhad distribučnej zložky 0,052 €/kWh), nie na celú cenu. Energia zostávajúca
v batérii na konci horizontu sa oceňuje priemernou cenou nasledujúcich 24 hodín
krát účinnosť vybíjania. Opotrebenie batérie sa rátá 0,01 €/kWh
(`SOFAR_BATTERY_WEAR_EUR_KWH`). Rozpis zobrazuje hodinovú tarifu, odhad FVE a
spotreby, SOC, nabíjanie/odber zo siete a odhadovaný náklad. Fixný mesačný
poplatok 1,50 € sa nemení podľa použitia, preto ho výpočet vynecháva.

Po výslovnom potvrdení sa plán uloží na server. Server v odporúčanom čase
prepne menič do nabíjania pri najviac 2 kW a na konci okna pošle príkaz
Self-use. Do meniča sa neposiela budúci TOU časovač; plán vykonáva aplikácia,
preto musí server a spojenie so SofarCloud zostať dostupné. Plán sa uchováva
v `SOFAR_DATA_DIR` (predvolene `./data`, v Dockeri v perzistentnom volume).
Po reštarte sa aktívny plán obnoví: ak čakajúci plán už vstúpil do okna, appka
spustí nabíjanie pri návrate; po uplynutí okna ho už nespustí. Pri obnovenom
nabíjaní appka zachová čas ukončenia. Ručné prepnutie do Self-use zruší aktívny
plán; iné ručné režimy sa počas neho odmietnu. Ak návrat do Self-use zlyhá,
appka opakuje pokus a v rozhraní ukazuje stav.

### Naučená predpoveď výroby a spotreby

Model sa učí z vlastnej histórie elektrárne a z archívu predpovedí Open-Meteo, takže
sklon, azimut ani tieň sa nemusia zadávať ručne. Pri štarte a potom raz za 7 dní sa
natrénuje zo SofarCloud hodinových meraní výroby a spotreby a uloží do
`data/forecast_model.json`. Stav a čas ďalšieho tréningu je v `GET /api/forecast-model`.

- **Výroba FVE:** pomer nameranej výroby a žiarenia sa učí zvlášť pre každú polohu
  slnka (výška a smer) a pre jasné a oblačné hodiny. Tieň, orientácia a veľkosť
  systému sa tak zachytia z dát. Riedke polohy sa vyrovnávajú k celkovému pomeru.
- **Spotreba:** denná spotreba je lineárny model podľa typu dňa (pracovný deň,
  sobota, nedeľa) a stupňových dní kúrenia a chladenia; základy teplôt sa tiež učia
  z dát. Hodinový profil je medián posledných šiestich týždňov a úroveň sa prispôsobuje
  posledným 14 dňom.
- **Režim** `SOFAR_FORECAST_MODEL_MODE`: `active` (predvolené) plánuje s modelom;
  `shadow` ukladá predpovede modelu do každého plánu, ale plánuje so vzorcom; `off`
  model nepoužíva. Ak model chýba alebo sa nedá vypočítať, plán sa vráti k vzorcu.

Výsledky na skúšobnej elektrárni Jacovce: 8 133 meraných hodín od 22. 11. 2025.
Posledných 28 dní (3. 10.–30. 10. 2026) sa nepoužilo na tréning a slúžilo na test.
Predpovede počasia sú archivované, nie skutočné namerané počasie.

| Metrika | Vzorec | Model |
|---|---|---|
| Výroba FVE, hodinová MAE | 0,268 kWh | **0,157 kWh** |
| Výroba FVE, denná MAE | 2,83 kWh | **2,40 kWh (−15 %)** |
| Výroba FVE, denný posun | −1,96 kWh | −0,98 kWh |
| Spotreba, denná MAE | 2,29 kWh | **2,10 kWh (−8 %)** |
| Spotreba, denný posun | −1,85 kWh | +0,71 kWh |
| Spotreba, hodinová MAE | 0,249 kWh | 0,271 kWh |

Vzorec je v tabuľke v pôvodnej podobe, s hodinovým posunom žiarenia. Po jeho oprave
je hodinová MAE výroby vzorca 0,176 kWh, model je stále lepší. Spotreba je lepšia
v dennom súčte, hodinový tvar je o niečo horší, preto pri plánovaní sleduje hlavne
energiu za deň.

Naučené parametre spotreby k 30. 10. 2026: pracovný deň 6,8 kWh, sobota 7,4 kWh,
nedeľa 6,3 kWh; kúrenie 0,75 kWh na stupeň pod 15 °C; chladenie 0,52 kWh na stupeň
nad 24 °C.

Obmedzenia: test pokrýva len jeden mesiac (október), sviatky a sneh model nepozná
a výsledky sa zlepšia s dlhšou históriou. Tréning spusti v Dockeri so `.env`:
`docker run --rm --env-file .env -v "$PWD":/app -w /app --entrypoint python sofar-control-sofar-opti train_forecast.py --location Jacovce --output /tmp/forecast_model.json`.

### História a namerané údaje

Aplikácia ukladá zmeny a potvrdenia režimu meniča, zlyhané povely, vypočítané
odporúčania vrátane modelovaného odhadu úspory a udalosti aktívneho plánu
(naplánovanie, štart, návrat do Self-use, zrušenie alebo chyba). Telemetriu
ukladá každých päť minút: výkon FVE, spotreby, batérie a siete, SOC, kapacitu
batérie a denné počítadlá energie. História sa ukladá v SQLite databáze
`SOFAR_DATA_DIR/history.sqlite3` (predvolene `./data`; v Dockeri v
perzistentnom volume) a uchováva sa bez automatického mazania. Rozhranie
zobrazuje najnovších 50 záznamov; API umožňuje najviac 200.

Pri výpočte plánu si aplikácia z SofarCloud stiahne historický výkon domu
(`usePower`, päťminútové body) za dostupné posledné obdobie, najviac 365 dní,
a uloží ho do rovnakej SQLite databázy. Synchronizácia sa opakuje najviac raz
za 24 hodín; pri ďalšom načítaní obnoví posledné dni s prekrytím, aby sa
zachytili oneskorené údaje. Dostupnosť starších meraní závisí od SofarCloud
retencie; ak sťahovanie zlyhá, plán zobrazí upozornenie a použije už uložené
merania alebo sezónny/rovnomerný záložný odhad.

Cena odberu za interval sa odhaduje z rozdielu denného počítadla importu medzi
dvoma vzorkami a vykazuje oba varianty tarify — s energopomocou aj bez nej.
Prvá vzorka, zmena dňa alebo pokles počítadla vytvorí nulový prírastok.
Tieto náklady sú len orientačné: závisia od dostupnosti a aktuálnosti dát
SofarCloud a nie sú fakturačným meraním. Forecast úspory je modelovaný odhad.
Po aplikovaní plánu sa po skončení jeho horizontu uloží porovnanie s
telemetriou a simulovaný kontrafaktuál Self-use; úspora stále nie je priamo
nameraná ani garantovaná.

Ide o prvý orientačný model, nie predikciu ani garanciu úspory. Denná spotreba
sa odhaduje mediánom z posledných štyroch úplných dní rovnakého dňa v týždni;
ak sú dostupné menej než dva také dni, použije sa medián až troch posledných
úplných dní. Deň sa považuje za úplný, ak existuje telemetrická vzorka o 23:30
alebo neskôr. Kým taká história nie je k dispozícii, použije sa nastavený
sezónny odhad. Denná spotreba sa rozdelí podľa historického hodinového profilu, ak sú
dostupné aspoň dva úplné dni s dostatkom vzoriek (aspoň 6 v každej hodine a
aspoň 18 pokrytých hodín za deň). Profil vychádza z priemerného výkonu domu
SofarCloud `use_power` v každej hodine a upraví sa tak, aby jeho súčet zodpovedal
dennému odhadu. Inak sa spotreba rozdelí rovnomerne. Ak je dostupný naučený model
(režim `active`, pozri nižšie), výroba a spotreba sa berú z neho. Inak predpoveď
žiarenia určuje odhad výroby FVE a spotreba sa odhaduje vyššie uvedeným spôsobom.
Ak je nastavený `SOFAR_PV_TILT_DEG`, žiarenie sa berie na naklonenú
plochu (Open-Meteo GTI) s azimutom `SOFAR_PV_AZIMUTH_DEG` (0 = juh, −90 = východ,
90 = západ); inak sa používa horizontálne globálne žiarenie. Exportné výnosy sú
predvolene 0 (`SOFAR_EXPORT_PRICE_EUR_KWH`). Denné riadenie vybíjania alebo
predaja do siete zatiaľ nie je automatizované.

Porovnanie stratégií na nameraných dňoch (bez zmeny meniča) spúšťa
`python backtest.py --db data/history.sqlite3 --days 30`; bez databázy
`python backtest.py --synthetic --days 14`. Výstup porovnáva Self-use, plán
z predchádzajúceho dňa, plán s dokonalou predpoveďou a to isté s režimom Hold.

### Vyhodnotenie výsledkov plánov

Pri potvrdení plánu sa uloží jedinečný identifikátor a úplný hodinový forecast.
Po skončení nabíjacích okien menič prejde do Self-use; vyhodnotenie pokračuje
zbieraním telemetrie až do konca zvoleného horizontu. Počas tohto zberu sa stav
plánu zobrazuje ako „vyhodnocuje telemetriu“. Nahradené alebo zrušené plány sa
zaznamenajú s dostupnou čiastočnou telemetriou.

Rozkliknuteľný výsledok porovnáva predpoveď so skutočnými päťminútovými
meraniami: výrobu a spotrebu počíta integráciou nameraného výkonu, import a
náklady odvodzuje z prírastkov počítadla importu a tarify a SOC preberá z
telemetrie. Zobrazuje aj pokrytie meraním, aby bolo viditeľné, keď chýbajú
vzorky. „Self-use“ v porovnaní je kontrafaktuálna simulácia s nameranou výrobou,
spotrebou, počiatočným SOC a tarifou; menič nemôže súčasne merať oba režimy.
Rozdiel sa porovnáva len na intervaloch s použiteľnou telemetriou a je preto
modelovaný odhad, nie priamo nameraná ani garantovaná úspora. Výsledky plánov
načítava API `/api/optimization-results`;
udalosti a posledné telemetrické vzorky zostávajú dostupné cez `/api/history`.

### Nepretržitá optimalizácia

V sekcii plánovania možno zapnúť automatické prepočítavanie. Automatika používa
24-hodinový posuvný horizont a prepočet spúšťa približne každú hodinu o :55,
aby nový plán mohol začať od najbližšej štvrthodiny. Ak už práve prebieha
nabíjacie okno, prepočet ho neprepíše a počká na ďalší hodinový cyklus. Menič sa
tak v automatike mení najviac raz za hodinu, pretože API SofarCloud je pomalé.
Nový výpočet aktualizuje jeden aktívny plánovací záznam; nevytvára nový
historický záznam pri každom hodinovom prepočte. Server a spojenie so
SofarCloud musia byť dostupné; stav zapnutia, nastavenia a priebežná história
sa ukladajú do SQLite databázy. Reštart servera preto automatiku obnoví.

Vypnutie automatiky zruší jej plán a pokúsi sa vrátiť menič do Self-use.
Aktívny záznam sa uzavrie a zostane v histórii s vyhodnotením telemetrie za
celý čas behu.
Ručná zmena režimu na Self-use automatiku tiež vypne; iné ručné režimy a
plánovanie na požiadanie sú počas jej aktivity blokované, aby si automatika
navzájom neprepisovala povely. Plánovací horizont je v automatike pevne 24
hodín; samostatné nastavenie 12/24/36 hodín sa naďalej používa pre plán na
požiadanie.

Prehľad výsledkov umožňuje zvoliť obdobie 1 až 30 dní. Zobrazuje náklad v
aktívnych intervaloch, simulovaný Self-use a ich rozdiel. Self-use sa
simuluje podľa nameranej výroby a spotreby a počiatočného SOC na začiatku
každého súvislého zapnutého obdobia; úspora je preto odhad, nie priamo
nameraná hodnota. Pokrytie telemetriou a počet aktívnych hodín sa zobrazujú
ako kontext výsledku. API: `/api/continuous-optimization` a
`/api/continuous-results?days=7`.

Dashboard priebežne zobrazuje tri SVG grafy bez externej grafovej knižnice:
24-hodinový priebeh výkonových tokov, dennú a kumulatívnu odhadovanú úsporu
nepretržitej optimalizácie a porovnanie predpovede so skutočnosťou pri
vyhodnotených plánoch. Detail každého plánu zobrazuje vedľa seba predikované
náklady s plánom a v Self-use a po vyhodnotení aj nameraný náklad plánu oproti
Self-use simulácii na rovnakých intervaloch s dostupným nákladovým meraním.
Skutočný Self-use režim počas toho istého plánu nemožno súčasne merať, preto
ide o kontrafaktuálny model; jeho pokrytie je zobrazené pri porovnaní.
Pri nepretržitej optimalizácii je predikcia najnovší rolling plán, zatiaľ čo
skutočný výsledok po vypnutí sumarizuje celý čas zapnutého behu.
Údaje výkonového grafu poskytuje
`/api/telemetry?hours=24`; merania sa obnovujú každých päť minút. Znamienko
výkonu siete je ponechané podľa telemetrie meniča a pri výpadkoch sa v grafe
nevykresľuje spojitá odhadovaná čiara. Podrobné hodinové tabuľky plánov sú
zbalené pod rozklikávacím detailom.

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

Pred naplánovaním aj manuálnym príkazom sa zobrazí potvrdenie; nabíjanie sa odmietne, ak aktuálne
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
Aktívny plán sa uchová v pomenovanom Docker volume `sofar-opti-data`, aby sa
dal obnoviť po reštarte kontajnera.

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
