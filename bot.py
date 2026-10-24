import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Literal, Optional
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

# Sett opp oversiktlig logging til både terminalen og en egen loggfil (bot.log)
LOG_FILE = Path(__file__).parent / "bot.log"
logger = logging.getLogger("baldarinfromasj")
logger.setLevel(logging.INFO)
_formatter = logging.Formatter(
    "[%(asctime)s] [%(levelname)-7s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)
if not logger.handlers:
    _console_handler = logging.StreamHandler()
    _console_handler.setFormatter(_formatter)
    _file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    _file_handler.setFormatter(_formatter)
    logger.addHandler(_console_handler)
    logger.addHandler(_file_handler)

TOKEN = os.getenv("DISCORD_TOKEN")
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Oslo"))
SCHEDULES_FILE = Path(__file__).parent / "schedules.json"

WIKI_LANGUAGES = {
    "en": "Engelsk (en.wikipedia.org)",
    "no": "Norsk Bokmål (no.wikipedia.org)",
    "nn": "Norsk Nynorsk (nn.wikipedia.org)",
}


def load_schedules() -> dict:
    if SCHEDULES_FILE.exists():
        try:
            return json.loads(SCHEDULES_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"Kunne ikke lese schedules.json: {e}")
            return {}
    return {}


def save_schedules(data: dict) -> None:
    SCHEDULES_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def has_time_passed(current_hm: str, target_hm: str) -> bool:
    """
    Sjekker om nåværende klokkeslett (HH:MM) har nådd eller passert målet (HH:MM) i dag.
    Dette gjør at hvis PC-en eller nettverket hadde et kort brudd akkurat på minuttet
    (f.eks. 00:00), så kjøres navnebyttet så snart forbindelsen er tilbake (f.eks. 00:01).
    """
    return current_hm >= target_hm


def parse_retry_after(header_val: Optional[str], default_seconds: float = 5.0) -> float:
    """
    Tolker Retry-After-headeren (enten antall sekunder eller HTTP-dato).
    Hvis headeren mangler eller er ugyldig, returneres `default_seconds` (5.0s).
    """
    if not header_val:
        return default_seconds
    try:
        return max(float(header_val), 0.1)
    except ValueError:
        try:
            retry_dt = parsedate_to_datetime(header_val)
            if retry_dt.tzinfo is None:
                retry_dt = retry_dt.replace(tzinfo=timezone.utc)
            diff = (retry_dt - datetime.now(timezone.utc)).total_seconds()
            return max(diff, default_seconds)
        except Exception:
            return default_seconds


async def fetch_random_wiki_titles(
    count: int = 1, lang: str = "en", max_length: int = 32
) -> list[str]:
    """
    Henter tilfeldige artikkel-titler fra Wikipedia (hoved-navnerommet, ns=0)
    som er maks `max_length` tegn lange. Trekker automatisk nye hvis en tittel er for lang.
    """
    url = f"https://{lang}.wikipedia.org/w/api.php"
    headers = {
        "User-Agent": "BaldarinfromasjBot/1.1 (https://github.com/baldarinfromasj/discord-wikinick; baldarinfromasj@gmail.com) aiohttp/3.14"
    }
    valid_titles: list[str] = []
    timeout = aiohttp.ClientTimeout(total=15)

    logger.info(
        f"[Wikipedia] Ber om {count} tilfeldig(e) tittel/titler fra {lang}.wikipedia.org (maks {max_length} tegn)..."
    )

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        attempts = 0
        while len(valid_titles) < count and attempts < 15:
            attempts += 1
            needed = count - len(valid_titles)
            # Be om ekstra artikler i ett kall slik at vi kan filtrere bort de over 32 tegn
            batch_size = min(max(needed * 3, 5), 500)
            params = {
                "action": "query",
                "format": "json",
                "list": "random",
                "rnnamespace": 0,  # Kun ekte artikler (ikke diskusjonssider, kategorier osv.)
                "rnlimit": batch_size,
            }
            try:
                async with session.get(url, params=params) as resp:
                    if resp.status == 429:
                        raw_retry = resp.headers.get("Retry-After")
                        wait_seconds = parse_retry_after(raw_retry, default_seconds=5.0)
                        if raw_retry:
                            logger.warning(
                                f"[Wikipedia] Rate-limit (HTTP 429) fra Wikipedia (forsøk {attempts}/15) – "
                                f"følger Retry-After-header ({raw_retry}) og venter {wait_seconds:.1f}s..."
                            )
                        else:
                            logger.warning(
                                f"[Wikipedia] Rate-limit (HTTP 429) uten Retry-After-header (forsøk {attempts}/15) – "
                                f"venter {wait_seconds:.1f}s før neste forsøk..."
                            )
                        await asyncio.sleep(wait_seconds)
                        continue
                    if resp.status != 200:
                        logger.warning(
                            f"[Wikipedia] API svarte med HTTP {resp.status} (forsøk {attempts}/15) – venter 5.0s..."
                        )
                        await asyncio.sleep(5.0)
                        continue
                    data = await resp.json()
                    random_pages = data.get("query", {}).get("random", [])
                    for page in random_pages:
                        title = page.get("title", "").strip()
                        if not title:
                            continue
                        if len(title) > max_length:
                            logger.info(
                                f"[Wikipedia] Forkastet '{title}' ({len(title)} tegn > maks {max_length})"
                            )
                            continue
                        if title in valid_titles:
                            logger.info(
                                f"[Wikipedia] Forkastet '{title}' (duplikat i samme trekning)"
                            )
                            continue

                        valid_titles.append(title)
                        logger.info(
                            f"[Wikipedia] Godkjent '{title}' ({len(title)} tegn) [{len(valid_titles)}/{count}]"
                        )
                        if len(valid_titles) >= count:
                            break
            except Exception as e:
                logger.error(
                    f"[Wikipedia] Feil ved henting fra API (forsøk {attempts}/15): {e} – venter 5.0s..."
                )
                await asyncio.sleep(5.0)

    if len(valid_titles) < count:
        logger.error(
            f"[Wikipedia] Klarte kun å hente {len(valid_titles)} av {count} ønskede titler etter {attempts} forsøk."
        )

    return valid_titles


async def change_members_nicknames(
    members: list[discord.Member], lang: str = "en", source: str = "Manuell kommando"
) -> tuple[list[tuple[discord.Member, str, str]], list[tuple[discord.Member, str]]]:
    """
    Endrer kallenavn på en liste med medlemmer til tilfeldige Wikipedia-artikler.
    Returnerer (vellykkede, feilede).
    """
    if not members:
        return [], []

    titles = await fetch_random_wiki_titles(count=len(members), lang=lang)
    if len(titles) < len(members):
        msg = f"Kunne ikke hente nok titler fra Wikipedia (fikk {len(titles)} av {len(members)})."
        logger.error(f"[{source}] {msg}")
        return [], [(m, msg) for m in members]

    succeeded: list[tuple[discord.Member, str, str]] = []
    failed: list[tuple[discord.Member, str]] = []

    for member, new_nick in zip(members, titles):
        old_name = member.display_name
        try:
            await member.edit(
                nick=new_nick,
                reason=f"Tilfeldig Wikipedia-artikkel ({lang}.wikipedia.org)",
            )
            succeeded.append((member, old_name, new_nick))
            logger.info(
                f"[{source}] Endret kallenavn for {member} (ID: {member.id}): "
                f"'{old_name}' -> '{new_nick}' ({lang}.wikipedia.org)"
            )
        except discord.Forbidden:
            reason = (
                "Mangler tillatelse (personen er enten server-eier, eller har en rolle "
                "som ligger HØYERE eller LIKT med Baldarinfromasj i rollelisten)"
            )
            failed.append((member, reason))
            logger.warning(
                f"[{source}] FEILET (Forbidden) for {member} ('{old_name}'): {reason}"
            )
        except discord.HTTPException as e:
            reason = f"Discord HTTP-feil ({e.status}): {e.text}"
            failed.append((member, reason))
            logger.error(f"[{source}] FEILET (HTTPException) for {member} ('{old_name}'): {reason}")
        except Exception as e:
            reason = f"Uventet feil: {e}"
            failed.append((member, reason))
            logger.error(f"[{source}] FEILET (Exception) for {member} ('{old_name}'): {reason}")

        # Liten pause hvis vi oppdaterer mange, for å respektere Discords rate limits
        if len(members) > 3:
            await asyncio.sleep(0.5)

    return succeeded, failed


def format_results(
    succeeded: list[tuple[discord.Member, str, str]],
    failed: list[tuple[discord.Member, str]],
    lang: str,
) -> str:
    lines: list[str] = []
    if succeeded:
        lines.append(f"**Oppdaterte {len(succeeded)} medlem(mer) (`{lang}.wikipedia.org`):**")
        for member, old_name, new_nick in succeeded[:25]:
            wiki_link = f"https://{lang}.wikipedia.org/wiki/{new_nick.replace(' ', '_')}"
            lines.append(f"• **{old_name}** → **[{new_nick}](<{wiki_link}>)** ({member.mention})")
        if len(succeeded) > 25:
            lines.append(f"*...og {len(succeeded) - 25} flere!*")

    if failed:
        if lines:
            lines.append("")
        lines.append(f"**Kunne ikke oppdatere {len(failed)} medlem(mer):**")
        for member, reason in failed[:10]:
            lines.append(f"• **{member.display_name}**: {reason}")
        if len(failed) > 10:
            lines.append(f"*...og {len(failed) - 10} flere.*")

    return "\n".join(lines) if lines else "Ingen medlemmer ble oppdatert."


class BaldarinfromasjBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True  # Nødvendig for å hente medlemslister og roller
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        await self.tree.sync()
        self.daily_nickname_scheduler.start()

    async def on_ready(self) -> None:
        logger.info(f"Logget inn som {self.user} (ID: {self.user.id})")
        logger.info("Baldarinfromasj er klar til å dele ut Wikipedia-navn!")
        schedules = load_schedules()
        total_auto = sum(len(g) for g in schedules.values())
        logger.info(f"Aktive automatiske tidsplaner lastet inn: {total_auto} stk.")

    @tasks.loop(seconds=30)
    async def daily_nickname_scheduler(self) -> None:
        """
        Sjekker hvert 30. sekund om noen planlagte navneendringer skal kjøres.
        Bruker `current_hm >= target_time` slik at et kort nettverksbrudd eller
        PC-dvale akkurat ved midnatt ikke fører til at dagen hoppes over!
        """
        try:
            now = datetime.now(TIMEZONE)
            current_hm = now.strftime("%H:%M")
            today_str = now.strftime("%Y-%m-%d")

            schedules = load_schedules()
            changed = False

            for guild_id_str, guild_schedules in schedules.items():
                guild = self.get_guild(int(guild_id_str))
                if not guild:
                    continue

                for user_id_str, entry in list(guild_schedules.items()):
                    target_time = entry.get("time", "00:00")
                    last_run = entry.get("last_run")
                    lang = entry.get("lang", "en")

                    if has_time_passed(current_hm, target_time) and last_run != today_str:
                        member = guild.get_member(int(user_id_str))
                        if not member:
                            try:
                                member = await guild.fetch_member(int(user_id_str))
                            except discord.NotFound:
                                logger.warning(
                                    f"[Auto-Scheduler] Fant ikke medlem med ID {user_id_str} på serveren '{guild.name}'."
                                )
                                entry["last_run"] = today_str
                                changed = True
                                continue
                            except discord.HTTPException as e:
                                logger.warning(
                                    f"[Auto-Scheduler] Midlertidig nettverksfeil ved henting av medlem {user_id_str}: {e}"
                                )
                                continue

                        succeeded, failed = await change_members_nicknames(
                            [member], lang=lang, source=f"Auto-Scheduler kl. {target_time}"
                        )
                        if succeeded:
                            entry["last_run"] = today_str
                            entry["name"] = succeeded[0][2]
                            changed = True
                        elif failed and "Mangler tillatelse" in failed[0][1]:
                            # Ikke prøv igjen hvert 30. sekund resten av dagen hvis rollen blokkerer det
                            entry["last_run"] = today_str
                            changed = True

            if changed:
                save_schedules(schedules)
        except Exception as e:
            logger.error(f"Uventet feil i daily_nickname_scheduler: {e}", exc_info=True)

    @daily_nickname_scheduler.before_loop
    async def before_scheduler(self) -> None:
        await self.wait_until_ready()


bot = BaldarinfromasjBot()

wiki_group = app_commands.Group(
    name="wikinick",
    description="Endre kallenavn til tilfeldige Wikipedia-artikler",
    default_permissions=discord.Permissions(manage_nicknames=True),
)


@wiki_group.command(name="person", description="Gi én bestemt person et tilfeldig Wikipedia-navn")
@app_commands.describe(
    medlem="Personen som skal få nytt navn",
    språk="Hvilket Wikipedia-språk (standard: engelsk)",
)
async def wikinick_person(
    interaction: discord.Interaction,
    medlem: discord.Member,
    språk: Literal["en", "no", "nn"] = "en",
) -> None:
    await interaction.response.defer(ephemeral=True)
    succeeded, failed = await change_members_nicknames(
        [medlem], lang=språk, source=f"/wikinick person (av {interaction.user})"
    )
    await interaction.followup.send(format_results(succeeded, failed, språk), ephemeral=True)


@wiki_group.command(
    name="flere",
    description="Gi flere bestemte personer tilfeldige Wikipedia-navn (f.eks. '@Hein @Ola @Kari')",
)
@app_commands.describe(
    medlemmer="Tagg personene du vil endre navn på (f.eks. @person1 @person2)",
    språk="Hvilket Wikipedia-språk (standard: engelsk)",
)
async def wikinick_flere(
    interaction: discord.Interaction,
    medlemmer: str,
    språk: Literal["en", "no", "nn"] = "en",
) -> None:
    if not interaction.guild:
        await interaction.response.send_message("Denne kommandoen må brukes på en server.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    user_ids = [int(uid) for uid in re.findall(r"<@!?(\d+)>", medlemmer)]
    # Fjern duplikater men behold rekkefølge
    unique_ids = list(dict.fromkeys(user_ids))

    if not unique_ids:
        await interaction.followup.send(
            "Fant ingen taggede brukere. Skriv f.eks. `/wikinick flere medlemmer: @Hein @Per`.",
            ephemeral=True,
        )
        return

    target_members: list[discord.Member] = []
    for uid in unique_ids:
        member = interaction.guild.get_member(uid)
        if not member:
            try:
                member = await interaction.guild.fetch_member(uid)
            except discord.NotFound:
                continue
        if member:
            target_members.append(member)

    succeeded, failed = await change_members_nicknames(
        target_members, lang=språk, source=f"/wikinick flere (av {interaction.user})"
    )
    await interaction.followup.send(format_results(succeeded, failed, språk), ephemeral=True)


@wiki_group.command(
    name="rolle",
    description="Gi alle medlemmer med én eller flere bestemte roller nye Wikipedia-navn",
)
@app_commands.describe(
    rolle="Rollen som skal få nye navn",
    ekstra_rolle="Valgfri ekstra rolle hvis du vil endre flere roller samtidig",
    språk="Hvilket Wikipedia-språk (standard: engelsk)",
    inkluder_boter="Om boter med rollen også skal få nytt navn (standard: Nei)",
)
async def wikinick_rolle(
    interaction: discord.Interaction,
    rolle: discord.Role,
    ekstra_rolle: Optional[discord.Role] = None,
    språk: Literal["en", "no", "nn"] = "en",
    inkluder_boter: bool = False,
) -> None:
    if not interaction.guild:
        await interaction.response.send_message("Denne kommandoen må brukes på en server.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    # Sørg for at hele medlemslisten er lastet inn hvis serveren ikke er chunket ennå
    if not interaction.guild.chunked:
        await interaction.guild.chunk()

    members_dict: dict[int, discord.Member] = {}
    for r in [rolle, ekstra_rolle]:
        if r:
            for m in r.members:
                if inkluder_boter or not m.bot:
                    members_dict[m.id] = m

    target_members = list(members_dict.values())
    if not target_members:
        await interaction.followup.send("Fant ingen medlemmer med den valgte rollen.", ephemeral=True)
        return

    succeeded, failed = await change_members_nicknames(
        target_members, lang=språk, source=f"/wikinick rolle @{rolle.name} (av {interaction.user})"
    )
    await interaction.followup.send(format_results(succeeded, failed, språk), ephemeral=True)


@wiki_group.command(
    name="alle",
    description="Gi ALLE medlemmene på hele serveren hvert sitt tilfeldige Wikipedia-navn",
)
@app_commands.describe(
    språk="Hvilket Wikipedia-språk (standard: engelsk)",
    inkluder_boter="Om boter også skal få nytt navn (standard: Nei)",
)
async def wikinick_alle(
    interaction: discord.Interaction,
    språk: Literal["en", "no", "nn"] = "en",
    inkluder_boter: bool = False,
) -> None:
    if not interaction.guild:
        await interaction.response.send_message("Denne kommandoen må brukes på en server.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    if not interaction.guild.chunked:
        await interaction.guild.chunk()

    target_members = [
        m
        for m in interaction.guild.members
        if (inkluder_boter or not m.bot) and m.id != interaction.guild.owner_id
    ]

    if not target_members:
        await interaction.followup.send("Fant ingen medlemmer som kan oppdateres.", ephemeral=True)
        return

    succeeded, failed = await change_members_nicknames(
        target_members, lang=språk, source=f"/wikinick alle (av {interaction.user})"
    )
    await interaction.followup.send(format_results(succeeded, failed, språk), ephemeral=True)


# --- Automatisering (f.eks. nytt navn til Hein hver midnatt) ---

auto_group = app_commands.Group(
    name="auto",
    parent=wiki_group,
    description="Automatisk daglig Wikipedia-navn (f.eks. hver midnatt)",
)


@auto_group.command(
    name="legg_til",
    description="Gi en person automatisk nytt Wikipedia-navn hver dag til et fast klokkeslett",
)
@app_commands.describe(
    medlem="Personen som skal få nytt navn hver dag (f.eks. Hein)",
    klokkeslett="Klokkeslett i format HH:MM (standard: 00:00 for midnatt)",
    språk="Hvilket Wikipedia-språk (standard: engelsk)",
)
async def auto_legg_til(
    interaction: discord.Interaction,
    medlem: discord.Member,
    klokkeslett: str = "00:00",
    språk: Literal["en", "no", "nn"] = "en",
) -> None:
    if not interaction.guild:
        await interaction.response.send_message("Denne kommandoen må brukes på en server.", ephemeral=True)
        return

    if not re.match(r"^(?:[01]\d|2[0-3]):[0-5]\d$", klokkeslett):
        await interaction.response.send_message(
            "Ugyldig klokkeslett! Bruk formatet `TT:MM`, f.eks. `00:00` for midnatt eller `18:30`.",
            ephemeral=True,
        )
        return

    now_str = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    current_hm = datetime.now(TIMEZONE).strftime("%H:%M")
    # Hvis klokkeslettet allerede har vært i dag, setter vi last_run til i dag slik at den venter til klokkeslettet i morgen
    initial_last_run = now_str if current_hm >= klokkeslett else None

    schedules = load_schedules()
    guild_id = str(interaction.guild.id)
    schedules.setdefault(guild_id, {})[str(medlem.id)] = {
        "name": medlem.display_name,
        "time": klokkeslett,
        "lang": språk,
        "last_run": initial_last_run,
    }
    save_schedules(schedules)
    logger.info(
        f"[Auto-Scheduler] La til daglig navneendring for {medlem} ('{medlem.display_name}') kl. {klokkeslett} ({språk})"
    )

    await interaction.response.send_message(
        f"Sånn. **{medlem.display_name}** ({medlem.mention}) vil nå få et nytt tilfeldig navn "
        f"fra `{språk}.wikipedia.org` hver dag kl. **{klokkeslett}** ({TIMEZONE}).",
        ephemeral=True,
    )


@auto_group.command(
    name="fjern",
    description="Stopp automatisk daglig navneendring for en person",
)
@app_commands.describe(medlem="Personen som ikke lenger skal få automatisk nytt navn")
async def auto_fjern(interaction: discord.Interaction, medlem: discord.Member) -> None:
    if not interaction.guild:
        await interaction.response.send_message("Denne kommandoen må brukes på en server.", ephemeral=True)
        return

    schedules = load_schedules()
    guild_id = str(interaction.guild.id)
    if guild_id in schedules and str(medlem.id) in schedules[guild_id]:
        del schedules[guild_id][str(medlem.id)]
        save_schedules(schedules)
        logger.info(f"[Auto-Scheduler] Fjernet daglig navneendring for {medlem} ('{medlem.display_name}')")
        await interaction.response.send_message(
            f"🛑 Stoppet automatisk daglig navnebytte for **{medlem.display_name}**.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f"**{medlem.display_name}** har ingen aktiv automatisk navneendring.",
            ephemeral=True,
        )


@auto_group.command(
    name="liste",
    description="Vis alle aktive automatiske navneendringer på serveren",
)
async def auto_liste(interaction: discord.Interaction) -> None:
    if not interaction.guild:
        await interaction.response.send_message("Denne kommandoen må brukes på en server.", ephemeral=True)
        return

    schedules = load_schedules()
    guild_schedules = schedules.get(str(interaction.guild.id), {})
    if not guild_schedules:
        await interaction.response.send_message(
            "Det er ingen aktive tidsplaner på denne serveren.",
            ephemeral=True,
        )
        return

    lines = [f"**Aktive daglige navneendringer ({TIMEZONE}):**"]
    for user_id, info in guild_schedules.items():
        last_run = info.get("last_run") or "Aldri"
        lines.append(
            f"• <@{user_id}> — hver dag kl. **{info.get('time', '00:00')}** "
            f"(`{info.get('lang', 'en')}.wikipedia.org`, sist kjørt: `{last_run}`)"
        )
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    cmd_name = interaction.command.qualified_name if interaction.command else "ukjent"
    logger.error(
        f"[Slash-kommando] Feil under kjøring av '/{cmd_name}' (av {interaction.user}): {error}",
        exc_info=True,
    )


bot.tree.add_command(wiki_group)


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError(
            "Fant ikke DISCORD_TOKEN! Opprett en .env-fil med DISCORD_TOKEN=din_token_her"
        )
    # log_level=logging.WARNING demper de stadige "RESUMED session"-meldingene fra discord.gateway
    bot.run(TOKEN, log_level=logging.WARNING)
