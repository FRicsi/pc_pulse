# PC Pulse 0.3.2 – Windows gyűjtő

## Beállítások a dashboardból, monitorozás vezérlése

Meglévő telepítésnél egyszer futtasd az **Update-Dashboard.cmd** fájlt
rendszergazdaként. Ez telepíti az önálló **PC Pulse Control** háttérfeladatot,
frissíti a felületet és a vezérlőfájlokat. A konfigurációt, adatokat és a már
beállított vezérlőjelszót megőrzi; **nem futtat auditbeállítást**.
Ehhez a frissítéshez ne az Install-Background.cmd fájlt használd.

A Beállítások menü első használatakor állíts be legalább 12 karakteres jelszót.
Ezután két rész érhető el:

1. **Konfiguráció:** a lemezen tárolt config.json aktuális értékei betöltődnek.
   A meghajtók checkboxokkal választhatók, a további mappák és kihagyások soronként
   adhatók meg. Az olvasás, megőrzés, eseménykorlát és mérési időközök szerkeszthetők.
   Mentéskor az előző konfiguráció config.json.bak fájlba kerül. A módosítás
   újraindítás után érvényes; a Mentés és monitorozás újraindítása gomb ezt is elvégzi.
2. **Monitorozás / hibaelhárítás:** állapot, javasolt következő lépés, indítás,
   leállítás, újraindítás, automatikus indulás engedélyezése vagy letiltása.
   A vezérlő kizárólag a Background-Status, Start-Background, Stop-Background,
   Restart-Background, Enable-Autostart és Disable-Autostart .cmd fájlokat indítja.
   Telepítő, audit-script, tetszőleges parancs vagy eltávolítás nem indítható innen.

A vezérlő külön folyamat, ezért a monitorozás leállítása után is elérhető:
alapból **http://127.0.0.1:8766/**. Ez a cím a teljes felületet is kiszolgálja,
és a gyűjtőtől lekéri a dashboard adatait. Leállított gyűjtőnél a Beállítások
továbbra is működik. A szokásos dashboard cím **http://127.0.0.1:8765/** marad.
Egyedi portnál a vezérlő alapból a monitorport + 1; a config.json `controlPort`
mezőjével ettől eltérő, külön port is beállítható a telepítés előtt.

A szerkesztés mindig a kijelzett konfigurációs útvonalra vonatkozik.
SYSTEM alatt a felhasználóhoz csatolt hálózati meghajtók nem feltétlenül érhetők el.
Új mappa méretmérése működik a konfiguráció alapján; fájleseményeihez meglévő
Windows-audit szükséges. Az olvasás checkbox nem állítja át a Windows auditját.
A jelszó sózott scrypt hashként kerül a védett settings-auth.json fájlba.
Frissítés után a vezérlő újraindulása miatt új bejelentkezés szükséges lehet.

Ellenőrzés: `python -B -m unittest discover -s tests -v`, Windows/Edge alatt
`python -B tests/browser_dashboard.py` és `python -B tests/browser_settings.py`.
A beállítási böngészőteszt valódi HTTP- és fájlmentési útvonalat használ ideiglenes
adatokkal; a Windows indítás/leállítás mellékhatásait helyettesíti. A .cmd indítás
idézőjelezése külön, csak olvasási állapotlekérdezéssel is ellenőrzött.

## Meghajtó- és mappagrafikon, közös User szűrő

Az Áttekintés tárhelypaneljén egy meghajtóra kattintva csak annak változása látszik.
A színes, egymásra rakott területek a figyelt, egymást nem átfedő mappák logikai
méretváltozását mutatják; a csökkenés a nulla alá kerül. A fehér vonal a meghajtó
tényleges foglalásváltozása. A szürke maradék a fizikai foglalás és a mért logikai
mappaméretek eltérése, nem egy bizonyítottan azonosított mappa.

A gyűjtő a figyelt gyökerek első szintű almappáit is méri ugyanabban a bejárásban.
A bontáshoz legalább két közös, teljes mérés szükséges. A régi összesített adatokból
nem készül visszamenőleges mappabontás; addig a meghajtó összesített trendje látszik.
Az auditbeállítást ehhez nem kell újrafuttatni.

A fejléc User választója alapból minden domain minden rögzített felhasználóját mutatja.
A kiválasztás az eseményszámot, programstatisztikákat, eseménygrafikont, táblázatot,
CSV-exportot és az eseményekben érintett mappák listáját közösen szűri.
Azonos felhasználónév külön domainben külön érték. A helyi eseményszűrők törlése
megtartja a főszűrőt. A tárhely- és mappaméretek közös gépmérések: a Windows audit
nem ad írt bájtszámot, ezért ezek változása nem tulajdonítható egyetlen usernek.

Elkülönített ellenőrzés (az éles adatbázis és audit használata nélkül):
`python -B -m unittest discover -s tests -v`, majd Windows/Edge alatt
`python -B tests/browser_dashboard.py`.

Helyi dashboard Firefoxhoz. Python 3.10 vagy újabb szükséges (Windows Python launcher: `py`). Nincs pip-csomag vagy felhő. Ez a csomag nem telepít automatikusan Pythont.

## Első indítás

1. Csomagold ki a ZIP-et egy állandó helyre, például `C:\Tools\PC-Pulse`. Ne a ZIP-ből indítsd.
2. A `config.json` fájlban állítsd be a `watch` mappákat. Alapérték: C:\, I:\, O:\, S:\ teljes meghajtók. A nem létező mappát kihagyja és jelzi. Elsőre kis mappával tesztelj; nagy játékgyűjtemény felmérése sokáig tarthat.
3. Nyiss PowerShellt **rendszergazdaként**, és futtasd (az útvonalat módosítva):

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Tools\PC-Pulse\windows\Enable-Audit.ps1"
```

Ez bekapcsolja a Windows File System sikeres auditját, és öröklődő auditbejegyzést ad a felsorolt mappákhoz. Nem módosítja a hozzáférési engedélyeket. Az öröklést tiltó almappák külön beállítást igényelhetnek. NTFS audit szükséges; hálózati/FAT mappákra ez nem teljes megoldás.

4. A `Start-PC-Pulse.cmd` fájlt jobb kattintás → **Futtatás rendszergazdaként**. A megnyíló ablakot hagyd futni.
5. Firefox: **http://127.0.0.1:8765**. Az első mappaméret-felmérés után jelennek meg a méretek; trendhez két mérés kell. Méretfelmérés: 15 perc, auditolvasás: 10 másodperc.

A gyűjtő első naplóolvasáskor a Security napló aktuális végéről indul. Csak az utána keletkező fájlaudit eseményeket gyűjti; újraindítás után a mentett kurzort folytatja. Az audit bekapcsolásától a Windows Security naplója is növekedhet, saját Windows méretkorlátja szerint.

## Gyors teszt

Az elindított dashboard mellett hozz létre egy kis szövegfájlt egy figyelt mappában, írj bele, mentsd el. Várj 10–20 másodpercet. A Fájlaktivitás nézetben az útvonalra keresve látnod kell a szerkesztő programot és a Windows-fiókot. Ha nincs esemény, nézd meg az Áttekintés → Gyűjtő állapota panelt. „Security napló olvasása működik” önmagában nem igazolja, hogy a mappa auditja be van állítva.

## Automatikus indulás, ki/bekapcsolás (0.3.2)

Zárd be a kézzel futó gyűjtőt (Ctrl+C). A `config.json` mappáit még telepítés előtt állítsd be.

1. Meglévő Python 3.10+ elegendő, például S:\Python312\python.exe. A telepítő automatikusan keresi a `py` launcherrel, és saját, standard könyvtáras futtatókörnyezetet másol a védett telepítési mappába. A site-packages csomagokat nem másolja, nem szükségesek. Ez némi időt és lemezhelyet igényel.
2. `Install-Background.cmd`: jobb kattintás → Futtatás rendszergazdaként.
3. A telepítő a Program Files\PC-Pulse mappába másol, és SYSTEM-fiókkal futó, **rendszerindításra** ütemezett PC Pulse feladatot regisztrál. Az auditot is bekapcsolja a figyelt gyökereken, majd azonnal elindítja. Bejelentkezés nem szükséges. Ez Feladatütemező-feladat, nem a services.msc-ben szereplő Windows-szolgáltatás.
4. A Firefoxban ugyanúgy http://127.0.0.1:8765 címen éred el (egyedi port esetén a beállított port).

Vezérlőfájlok, az állapotlekérdezés kivételével rendszergazdaként:

| Fájl | Hatás |
|---|---|
| Start-Background.cmd | Most elindítja; az automatikus indulást nem módosítja |
| Stop-Background.cmd | Most leállítja; a következő rendszerindításkor újra indulhat |
| Disable-Autostart.cmd | Leállítja, és letiltja az automatikus indulást |
| Enable-Autostart.cmd | Engedélyezi az automatikus indulást és most elindítja |
| Background-Status.cmd | Feladat állapota és utolsó futási eredménye |

**Telepítés után** az érvényes konfiguráció: `C:\Program Files\PC-Pulse\config.json` (a Program Files tényleges helye szerint). A telepítéskor a `%USERPROFILE%` és `%LOCALAPPDATA%` a telepítő felhasználó konkrét útvonalára vált, így SYSTEM alatt is a megfelelő mappát figyeli. Újratelepítéskor a kicsomagolt csomag konfigurációját alkalmazza, a korábbi telepített konfigurációt config.json.bak néven elmenti. Szerkesztéshez rendszergazdaként indított szövegszerkesztő kell. Ezután Stop-Background, majd Start-Background. Új mappa auditját a telepített windows\Enable-Audit.ps1 scripttel állítsd be. Más fiók mappáját konkrét útvonallal add meg. Hálózati, felhasználóhoz csatolt meghajtókat SYSTEM nem feltétlenül lát; helyi meghajtókhoz készült.

A kézi gyűjtőt ne futtasd az automatikus mellett. Ha már vannak korábbi adataid, a telepítő első telepítéskor átmásolja a `data` mappát; előtte állítsd le a kézi gyűjtőt, hogy az adatbázis ne változzon másolás közben.

Eltávolítás (a telepített script rendszergazda PowerShellben):

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Program Files\PC-Pulse\windows\Control-Background.ps1" -Action Uninstall
```

Ez a feladatot törli; az adatok és a Windows auditbeállításai megmaradnak. A gyűjtés kikapcsolása után a Windows tovább auditálhat. Ha azt is leállítanád, a mappák Tulajdonságok → Biztonság → Speciális → Naplózás lapján távolítsd el a hozzáadott Everyone/Sikeres/írás-törlés (opcionálisan olvasás) bejegyzést. Más auditbejegyzéseket hagyj meg.

## Mit mér és mit nem?

- Meghajtók tényleges szabad/foglalt helye és annak változása az elérhető, legfeljebb 24 órás időszakban. Az eltérő meghajtókészlettel készült mintákat nem kapcsolja össze.
- Mappák logikai fájlmérete: tömörítés, ritka fájlok, hardlinkek miatt eltérhet a tényleges foglalástól. Nem követ junctiont vagy más reparse pointot. Olvashatatlan fájloknál részleges méretet jelez. Átfedő mappák számai nem adhatók össze.
- Security 4663: programútvonal, fiók, időpont, fájlútvonal, írás/olvasás/törlési hozzáférés. A törlési hozzáférés nem garantál befejezett törlést; az írás nem bizonyít új fájl létrehozást. Az audit nem ad írt bájtszámot.
- A programlista a betöltött legfeljebb 5000 esemény írási eseményszáma szerint rangsorol. Nem állítja, hogy egy program okozta a mappanövekedést. A teljes napi rögzített eseményszám külön látszik.
- Közös Windows-fióknál az ember nem azonosítható. Olvasás gyűjtése alapból kikapcsolva; `reads: true` után futtasd újra az audit-scriptet is.
- Nincs fájltartalom, billentyű vagy képernyő rögzítése. Nincs automatikus takarítás.

## Beállítások és adatok

`config.json`: figyelt mappák (`watch`), kihagyott részfák (`exclude`), olvasás (`reads`), megőrzés (`retentionDays`), eseménykorlát (`maxEvents`), időközök, port. Változtatás után indítsd újra a gyűjtőt. Új mappán az audit-scriptet is futtasd újra. A Beállítások menüben jelszavas belépés után szerkeszthetők; a mentés újraindítás után érvényes.

`data\pulse.sqlite`: helyi adatbázis; alapból 14 nap és legfeljebb 100 000 esemény, periodikus törléssel és helyfelszabadítással. Sok hosszú útvonal mellett ez is több tíz/száz MB lehet; nem merev bájtkorlát. A gyűjtő kizárja a saját adatmappáját. Az API csak 127.0.0.1-re köt, nem publikál a hálózatra. A helyi gép böngészőiből a dashboard olvasható.

## Ellenőrzés

A csomag Linuxon kapott Python adatfeldolgozási, API és JavaScript szintaktikai ellenőrzést. A Windows audit, jogosultság és az új rendszerindítási feladat/vezérlőscriptek működése helyi Windows-tesztet igényel. Az első futtatásnál a fenti kisfájlos tesztet végezd el.

Microsoft referencia: https://learn.microsoft.com/en-us/previous-versions/windows/it-pro/windows-10/security/threat-protection/auditing/event-4663

A teljes meghajtógyökerek Everyone auditja a később létrehozott felhasználók hozzáféréseire is vonatkozik, ha a mappa örökli az auditot. Ez nem igényel külön felhasználónkénti telepítést. Az öröklést tiltó mappák továbbra is kivételek; a dashboard nem ígér teljes körű, veszteségmentes naplózást nagy terhelés mellett. Teljes meghajtó auditjának beállítása is eltarthat egy ideig.

0.3.2: a telepítő csak a gyökérmappán tiltja az öröklést; a gyermekek öröklik a gyökér engedélyeit. A korábbi telepítő által létrehozott üres gyermek-ACL-eket frissítéskor /reset-tel javítja.

## 0.4 frissítés – időszakválasztó

Csomagold ki külön mappába, majd az **Update-Dashboard.cmd** fájlt futtasd rendszergazdaként. Ez csak a szervert és a felületet frissíti; nem futtatja újra a teljes meghajtók auditbeállítását, nem módosítja a konfigurációt és a megőrzött adatokat. A futó háttérfeladatot leállítja és újraindítja; ha eleve nem futott, frissítés után sem indítja. Firefoxban frissítsd az oldalt Ctrl+F5-tel.

A fejlécben 1–9 és óra/nap/hét/hónap/év választható, alapból 1 nap. A választást a böngésző megjegyzi. A hónap és év naptári időszak, UTC szerint; a hóvégi és szökőnapi dátumot az adott hónap utolsó napjára igazítja. Az aktuális szabad hely mindig a legfrissebb mérés, a változások és események a kiválasztott időszakhoz tartoznak. A programstatisztikák az összes megőrzött, időszakba eső eseményt számolják, míg a táblázat és a CSV legfeljebb 5000 legfrissebb eseményt tartalmaz.

A hosszú időszak kiválasztása nem növeli a naplómegőrzést. Alapból továbbra is 14 nap/100 000 esemény marad meg; a fejléc mutatja a megőrzést és a legkorábbi elérhető adat idejét. Hosszabb megőrzéshez a telepített config.json retentionDays mezőjét külön növeld, és indítsd újra a gyűjtőt. Az eseménykorlát ettől független: maxEvents hamarabb is levághatja a történetet. A már törölt adatokat a frissítés nem állítja vissza.
