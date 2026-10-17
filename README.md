# Baldarinfromasj – Wikipedia Discord Nickname Bot

En Discord-bot som endrer kallenavnet (nickname) til medlemmer på serveren til navnet på en tilfeldig Wikipedia-artikkel (maks 32 tegn).

## Funksjoner / Slash-kommandoer

* `/wikinick person medlem:@Navn [språk:en/no/nn]` — Endrer kallenavnet til **én bestemt person**.
* `/wikinick flere medlemmer:@Navn1 @Navn2 [språk:en/no/nn]` — Endrer kallenavnet til **flere bestemte personer** med ulike tilfeldige artikler.
* `/wikinick rolle rolle:@Rolle [ekstra_rolle:@Rolle2] [språk:en/no/nn]` — Endrer kallenavnet til **alle som har bestemte roller**.
* `/wikinick alle [språk:en/no/nn]` — Endrer kallenavnet til **alle på hele serveren** (unntatt server-eier).
* `/wikinick auto legg_til medlem:@Hein [klokkeslett:00:00] [språk:en/no/nn]` — Gir en person et nytt tilfeldig Wikipedia-navn **hver dag ved midnatt** (eller valgt klokkeslett).
* `/wikinick auto fjern medlem:@Hein` — Stopper daglig automatisk navnebytte.
* `/wikinick auto liste` — Viser hvem som får nytt navn automatisk hver dag.

---

## Steg 1: Opprett boten «Baldarinfromasj» på Discord

1. Gå til [Discord Developer Portal](https://discord.com/developers/applications) og logg inn.
2. Klikk på **New Application** øverst til høyre, og skriv inn navnet **`Baldarinfromasj`**.
3. Gå til **Bot** i menyen til venstre:
   * Her kan du også endre brukernavn og profilbilde til **Baldarinfromasj** når som helst.
   * Bla ned til **Privileged Gateway Intents** og slå **PÅ** **`Server Members Intent`** (viktig for at boten skal kunne se hvem som er på serveren og hvilke roller de har!). Klikk **Save Changes**.
   * Bla opp igjen og klikk på **Reset Token** (eller *Copy*) for å kopiere botens hemmelige Token.
4. Gå til **OAuth2** -> **URL Generator** i menyen til venstre:
   * Under **Scopes**, huk av for:
     * `bot`
     * `applications.commands`
   * Under **Bot Permissions** som dukker opp under, huk av for:
     * `Manage Nicknames` (Administrer kallenavn)
     * `Send Messages` (for å poste resultater og midnatts-oppdateringer)
   * Kopier **Generated URL** nederst på siden, lim den inn i nettleseren din, og inviter **Baldarinfromasj** til serveren din!
5. **Viktig inne på Discord-serveren din:**
   * Gå til *Serverinnstillinger -> Roller*, og dra rollen **Baldarinfromasj** **over** rollene til medlemmene den skal kunne endre navn på (f.eks. helt øverst).

---

## Steg 2: Kjør boten på PC-en din

1. **Installer Python** (hvis du ikke har det):
   * Last ned fra [python.org](https://www.python.org/downloads/) (husk å huke av for *"Add python.exe to PATH"* under installasjonen), eller kjør `winget install Python.Python.3.12` i terminalen.
2. **Installer pakkene:**
   ```powershell
   pip install -r requirements.txt
   ```
3. **Legg inn Token:**
   * Kopier filen `.env.example` til en ny fil som heter `.env`
   * Lim inn tokenet ditt etter `DISCORD_TOKEN=`
4. **Start boten:**
   ```powershell
   python bot.py
   ```
