import asyncio
import json
import os
import re
from datetime import datetime, time
from pathlib import Path
from typing import Literal, Optional
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

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
        except Exception:
            return {}
    return {}


def save_schedules(data: dict) -> None:
    SCHEDULES_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


async def fetch_random_wiki_titles(
    count: int = 1, lang: str = "en", max_length: int = 32
) -> list[str]:
    """
    Henter tilfeldige artikkel-titler fra Wikipedia (hoved-navnerommet, ns=0)
    som er maks `max_length` tegn lange. Trekker automatisk nye hvis en tittel er for lang.
    """
    url = f"https://{lang}.wikipedia.org/w/api.php"
    headers = {
        "User-Agent": "BaldarinfromasjDiscordBot/1.0 (https://discord.com; contact@example.com)"
    }
    valid_titles: list[str] = []

    async with aiohttp.ClientSession(headers=headers) as session:
        attempts = 0
        while len(valid_titles) < count and attempts < 15:
            attempts += 1
            needed = count - len(valid_titles)
            # Be om ekstra artikler i ett kall slik at vi kan filtrere bort de over 32 tegn
            batch_size = min(max(needed * 3, 10), 500)
            params = {
                "action": "query",
                "format": "json",
                "list": "random",
                "rnnamespace": 0,  # Kun ekte artikler (ikke diskusjonssider, kategorier osv.)
                "rnlimit": batch_size,
            }
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    break
                data = await resp.json()
                random_pages = data.get("query", {}).get("random", [])
                for page in random_pages:
                    title = page.get("title", "").strip()
                    if 1 <= len(title) <= max_length and title not in valid_titles:
                        valid_titles.append(title)
                        if len(valid_titles) >= count:
                            break

    return valid_titles


async def change_members_nicknames(
    members: list[discord.Member], lang: str = "en"
) -> tuple[list[tuple[discord.Member, str, str]], list[tuple[discord.Member, str]]]:
    """
    Endrer kallenavn på en liste med medlemmer til tilfeldige Wikipedia-artikler.
    Returnerer (vellykkede, feilede).
    """
    if not members:
        return [], []

    titles = await fetch_random_wiki_titles(count=len(members), lang=lang)
    if len(titles) < len(members):
        return [], [(m, "Kunne ikke hente nok titler fra Wikipedia.") for m in members]

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
        except discord.Forbidden:
            failed.append(
                (
                    member,
                    "Mangler tillatelse (server-eier, eller rollen er over Baldarinfromasj)",
                )
            )
        except discord.HTTPException as e:
            failed.append((member, f"Discord-feil: {e}"))

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
        print(f"Logget inn som {self.user} (ID: {self.user.id})")
        print("Baldarinfromasj er klar til å dele ut Wikipedia-navn!")

    @tasks.loop(minutes=1)
    async def daily_nickname_scheduler(self) -> None:
        """Sjekker hvert minutt om noen planlagte navneendringer skal kjøres (f.eks. ved midnatt)."""
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
                channel_id = entry.get("channel_id")

                if current_hm == target_time and last_run != today_str:
                    member = guild.get_member(int(user_id_str))
                    if not member:
                        try:
                            member = await guild.fetch_member(int(user_id_str))
                        except discord.NotFound:
                            continue

                    await change_members_nicknames([member], lang=lang)
                    entry["last_run"] = today_str
                    changed = True

        if changed:
            save_schedules(schedules)

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
    succeeded, failed = await change_members_nicknames([medlem], lang=språk)
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

    succeeded, failed = await change_members_nicknames(target_members, lang=språk)
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

    succeeded, failed = await change_members_nicknames(target_members, lang=språk)
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
    target_members = [
        m
        for m in interaction.guild.members
        if (inkluder_boter or not m.bot) and m.id != interaction.guild.owner_id
    ]

    if not target_members:
        await interaction.followup.send("Fant ingen medlemmer som kan oppdateres.", ephemeral=True)
        return

    succeeded, failed = await change_members_nicknames(target_members, lang=språk)
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

    schedules = load_schedules()
    guild_id = str(interaction.guild.id)
    schedules.setdefault(guild_id, {})[str(medlem.id)] = {
        "name": medlem.display_name,
        "time": klokkeslett,
        "lang": språk,
        "last_run": None,
    }
    save_schedules(schedules)

    await interaction.response.send_message(
        f"🤫 Satt opp i skjul! **{medlem.display_name}** ({medlem.mention}) vil nå få et nytt tilfeldig navn "
        f"fra `{språk}.wikipedia.org` hver dag kl. **{klokkeslett}** ({TIMEZONE}) helt uten at det postes noen melding.",
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
        lines.append(
            f"• <@{user_id}> — hver dag kl. **{info.get('time', '00:00')}** (`{info.get('lang', 'en')}.wikipedia.org`)"
        )
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


bot.tree.add_command(wiki_group)


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError(
            "Fant ikke DISCORD_TOKEN! Opprett en .env-fil med DISCORD_TOKEN=din_token_her"
        )
    bot.run(TOKEN)
