"""
Shungite Discord Bot v2 — support, economy, minigames, auto-roles, welcome, anti-spam.
Requires: discord.py 2.x
Run: python shungite_bot.py  (token in ~/.shungite_bot_token or env DISCORD_TOKEN)
"""
import json
import os
import random
import time
import asyncio
import discord
from discord.ext import commands

TOKEN_PATH = os.path.expanduser("~/.shungite_bot_token")
TOKEN = None
if os.path.exists(TOKEN_PATH):
    TOKEN = open(TOKEN_PATH).read().strip()
if not TOKEN:
    TOKEN = os.environ.get("DISCORD_TOKEN")

GITHUB_URL = "https://github.com/IrtezaAsif/Shungite"
RELEASES_URL = "https://github.com/IrtezaAsif/Shungite/releases/latest"
VILLAGER_ROLE = "Villager"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True

bot = commands.Bot(command_prefix=["sh!", "s!"], intents=intents, help_command=None)

# ---------------------------------------------------------------- persistent data
DATA_DIR = os.path.dirname(os.path.abspath(__file__))
XP_FILE = os.path.join(DATA_DIR, "bot_stats.json")

def _load(path):
    try:
        return json.load(open(path))
    except Exception:
        return {}

def _save(path, d):
    json.dump(d, open(path, "w"))

def add_xp(user_id, amount):
    s = _load(XP_FILE)
    u = s.setdefault(str(user_id), {"xp": 0, "coins": 100, "wins": 0, "streak": 0})
    u["xp"] += amount
    _save(XP_FILE, s)
    return 1 + int(u["xp"] // 100)

def add_coins(user_id, amount):
    s = _load(XP_FILE)
    u = s.setdefault(str(user_id), {"xp": 0, "coins": 100, "wins": 0, "streak": 0})
    u["coins"] = max(0, u["coins"] + amount)
    _save(XP_FILE, s)
    return u["coins"]

def get_user(user_id):
    d = _load(XP_FILE).get(str(user_id))
    if d is None:
        return {"xp": 0, "coins": 100, "wins": 0, "streak": 0}
    d.setdefault("coins", 100)
    d.setdefault("xp", 0)
    d.setdefault("wins", 0)
    return d

# ---------------------------------------------------------------- anti-spam
SPAM_TRACK = {}
SPAM_LIMIT = 6
SPAM_WINDOW = 8
MUTED = set()
_spam_warned = set()

def is_spam(user_id):
    now = time.time()
    if user_id in MUTED:
        return True
    hist = SPAM_TRACK.get(user_id, [])
    hist = [t for t in hist if now - t < SPAM_WINDOW]
    hist.append(now)
    SPAM_TRACK[user_id] = hist
    if len(hist) > SPAM_LIMIT:
        MUTED.add(user_id)
        asyncio.get_event_loop().call_later(20, lambda: (MUTED.discard(user_id), _spam_warned.discard(user_id)) and None)
        return True
    return False

# ---------------------------------------------------------------- events
@bot.event
async def on_ready():
    try:
        await bot.tree.sync()
    except Exception:
        pass
    print(f"Shungite v2 online as {bot.user} — {len(bot.guilds)} guild(s)", flush=True)
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.listening, name="sh!help | Shungite 🎵"))

@bot.event
async def on_member_join(member):
    """Auto-assign Villager role + welcome message in #general."""
    guild = member.guild
    role = discord.utils.get(guild.roles, name=VILLAGER_ROLE)
    if role:
        try:
            await member.add_roles(role, reason="Auto-assigned on join")
        except Exception:
            pass
    ch = discord.utils.get(guild.text_channels, name="general")
    if ch:
        try:
            e = discord.Embed(
                title=f"Welcome to Shungite, {member.display_name}! 🎉",
                description="Grab the installer, meet the crew, play some games.\n\n"
                             f"📦 [Download Shungite]({RELEASES_URL})\n"
                             "🎮 Type `sh!help` to see everything I can do.\n"
                             "🪙 Chat to earn coins — `sh!daily` for free ones!",
                color=0x2b6e37)
            e.set_thumbnail(url=member.display_avatar.url)
            await ch.send(content=f"{member.mention} just joined!", embed=e)
        except Exception:
            pass

# ---------------------------------------------------------------- games state
GAMES = {}   # channel_id -> hangman game
TRIVIA = [
    ("Which codec does Shungite download by default?", ["opus"]),
    ("What file makes batch runs resumable?", ["index", "_index"]),
    ("Which GPU tool syncs lyrics with no database?", ["whisperx", "whisper x"]),
    ("What other audio format does Shungite offer?", ["m4a"]),
    ("What tag stores the YouTube source link?", ["purl"]),
    ("Where does Shungite pull timed lyrics from?", ["youtube", "youtube captions", "captions"]),
    ("What's the lyric file format with timestamps?", ["lrc"]),
    ("What blocks downloads until you log in?", ["bot", "bot wall", "bot check", "bot-check"]),
    ("What are Shungite files named after?", ["artist", "artist - title"]),
    ("What does the GDPR zip import give you?", ["history", "listening history", "entire history"]),
]
TRIVIA_ACTIVE = {}   # channel_id -> trivia game
GUESS_ACTIVE = {}    # user_id -> number-guess game

@bot.event
async def on_message(msg):
    if msg.author.bot or not msg.guild:
        return
    # guess-mode: plain numbers reply to an active sh!guess game
    g = GUESS_ACTIVE.get(msg.author.id)
    if g and msg.content.strip().isdigit():
        n = int(msg.content.strip())
        g["tries"] -= 1
        if n == g["n"]:
            prize = 20 * g["tries"] + 20
            add_xp(msg.author.id, 15)
            add_coins(msg.author.id, prize)
            GUESS_ACTIVE.pop(msg.author.id, None)
            await msg.reply(f"🎉 {n} is correct! +{prize} 🪙")
            return
        if g["tries"] <= 0:
            GUESS_ACTIVE.pop(msg.author.id, None)
            await msg.reply(f"💀 Out of tries! It was {g['n']}.")
            return
        await msg.reply("📈 Higher!" if n < g["n"] else "📉 Lower!")
        return
    # anti-spam
    if is_spam(msg.author.id):
        try:
            await msg.delete()
            if msg.author.id not in _spam_warned:
                _spam_warned.add(msg.author.id)
                await msg.channel.send(
                    f"⚠️ {msg.author.mention} too fast! 20s cooldown.", delete_after=5)
        except Exception:
            pass
        return
    # XP + random coin drop for chatting
    add_xp(msg.author.id, 1)
    if random.random() < 0.12:
        won = random.randint(3, 15)
        add_coins(msg.author.id, won)
    await bot.process_commands(msg)

# ---------------------------------------------------------------- info
@bot.command()
async def help(ctx):
    e = discord.Embed(title="Shungite Bot — everything I do", color=0x2b6e37)
    e.add_field(name="🛠 Info", value="`sh!about` · `sh!download` · `sh!faq` · `sh!server`", inline=False)
    e.add_field(name="🎮 Games", value="`sh!hangman` · `sh!trivia` · `sh!guess` · `sh!typerace` · `sh!slots` · `sh!rps <r/p/s>` · `sh!8ball <q>` · `sh!roll` · `sh!coin`", inline=False)
    e.add_field(name="💰 Economy", value="`sh!balance` · `sh!daily` · `sh!work` · `sh!steal @user` · `sh!shop` · `sh!buy <item>`", inline=False)
    e.add_field(name="🧰 Utility", value="`sh!rank` · `sh!lb` · `sh!poll <q | opt1 | opt2>` · `sh!avatar [@user]` · `sh!randomsong`", inline=False)
    e.set_footer(text="New members auto-get the Villager role • chat to earn 🪙 • games pay more")
    await ctx.reply(embed=e)

@bot.command()
async def about(ctx):
    e = discord.Embed(title="Shungite", color=0x2b6e37,
        description="Free, open-source Windows app that downloads your entire Spotify library from YouTube "
                     "as best-quality Opus — `Artist - Title` names, cover art, synced lyrics, genre, "
                     "and the YouTube source link embedded in every file.")
    e.add_field(name="GitHub", value=GITHUB_URL, inline=False)
    e.add_field(name="Download", value=RELEASES_URL, inline=False)
    await ctx.reply(embed=e)

@bot.command()
async def download(ctx):
    await ctx.reply(f"📦 {RELEASES_URL}\nInstaller + uninstaller, no console windows. Windows 10/11.")

@bot.command()
async def faq(ctx):
    e = discord.Embed(title="FAQ", color=0x2b6e37)
    e.add_field(name="Downloads all fail", value="Settings → Login to YouTube, then retry.", inline=False)
    e.add_field(name="Frozen at 'Scanning'", value="Your CSV folder path is wrong — re-pick it.", inline=False)
    e.add_field(name="Lyrics missing", value="Track is likely instrumental — nothing to sync.", inline=False)
    e.add_field(name="Where is my data", value="`%LOCALAPPDATA%\\..\\LocalLow\\Shungite`", inline=False)
    await ctx.reply(embed=e)

@bot.command()
async def server(ctx):
    g = ctx.guild
    e = discord.Embed(title=g.name, color=0x2b6e37)
    e.add_field(name="Members", value=g.member_count)
    e.add_field(name="Created", value=g.created_at.strftime("%b %d, %Y"))
    e.add_field(name="Roles", value=len(g.roles))
    e.add_field(name="Channels", value=f"{len(g.text_channels)} text · {len(g.voice_channels)} voice")
    if g.icon:
        e.set_thumbnail(url=g.icon.url)
    await ctx.reply(embed=e)

@bot.command()
async def avatar(ctx, member: discord.Member = None):
    m = member or ctx.author
    await ctx.reply(m.display_avatar.url)

# ---------------------------------------------------------------- economy
@bot.command()
async def balance(ctx):
    u = get_user(ctx.author.id)
    lvl = 1 + int(u["xp"] // 100)
    e = discord.Embed(title=f"{ctx.author.display_name}'s wallet", color=0x2b6e37)
    e.add_field(name="🪙 Coins", value=u["coins"])
    e.add_field(name="⭐ Level", value=lvl)
    e.add_field(name="🏆 Wins", value=u.get("wins", 0))
    await ctx.reply(embed=e)

@bot.command()
async def daily(ctx):
    s = _load(XP_FILE)
    u = s.setdefault(str(ctx.author.id), {"xp": 0, "coins": 100, "wins": 0, "streak": 0})
    last = u.get("last_daily", 0)
    now = time.time()
    if now - last < 86400:
        wait = int(86400 - (now - last))
        await ctx.reply(f"⏳ Already claimed! Come back in {wait // 3600}h {(wait % 3600) // 60}m.")
        return
    u["streak"] = 1 if now - last < 86400 * 2 else u.get("streak", 0) + 1 if last else 1
    pay = 50 + 10 * min(u["streak"], 7)
    u["coins"] += pay
    u["last_daily"] = now
    _save(XP_FILE, s)
    await ctx.reply(f"💰 Daily claimed: **+{pay} 🪙** (streak {u['streak']} — come back tomorrow for more)")

@bot.command()
async def work(ctx):
    jobs = ["downloaded a playlist", "synced some lyrics", "fixed a bot-wall", "tagged 50 files",
            "converted .vtt to .lrc", "matched a cover song correctly", "retimed lyrics on GPU",
            "built an .m3u", "batch-imported a CSV", "embedded cover art"]
    pay = random.randint(10, 40)
    add_coins(ctx.author.id, pay)
    await ctx.reply(f"🛠 You {random.choice(jobs)} and earned **{pay} 🪙**")

@bot.command()
async def steal(ctx, target: discord.Member = None):
    if not target or target.bot or target.id == ctx.author.id:
        await ctx.reply("Pick someone to steal from: `sh!steal @user`")
        return
    if get_user(ctx.author.id)["coins"] < 20:
        await ctx.reply("You need at least 20 🪙 to attempt a heist.")
        return
    if random.random() < 0.4:
        loot = min(random.randint(10, 50), get_user(target.id)["coins"])
        add_coins(ctx.author.id, loot)
        add_coins(target.id, -loot)
        await ctx.reply(f"🥷 You stole **{loot} 🪙** from {target.mention}! Not nice.")
    else:
        fine = random.randint(10, 25)
        add_coins(ctx.author.id, -fine)
        await ctx.reply(f"🚨 Caught! You paid **{fine} 🪙** in damages to {target.mention}.")

@bot.command()
async def shop(ctx):
    e = discord.Embed(title="🛒 Shop", color=0x2b6e37)
    e.add_field(name="🎨 Custom color role — 500 🪙", value="`sh!buy color <name>`", inline=False)
    e.add_field(name="⭐ VIP role — 1000 🪙", value="`sh!buy vip`", inline=False)
    await ctx.reply(embed=e)

@bot.command()
async def buy(ctx, item=None, *, value=None):
    if item == "vip":
        if get_user(ctx.author.id)["coins"] < 1000:
            await ctx.reply("Need 1000 🪙.")
            return
        add_coins(ctx.author.id, -1000)
        role = discord.utils.get(ctx.guild.roles, name="VIP")
        if not role:
            role = await ctx.guild.create_role(name="VIP", color=discord.Color.from_str("#e5e4e2"))
        await ctx.author.add_roles(role)
        await ctx.reply("🎉 You're now **VIP**!")
        return
    if item == "color" and value:
        if get_user(ctx.author.id)["coins"] < 500:
            await ctx.reply("Need 500 🪙.")
            return
        add_coins(ctx.author.id, -500)
        role = await ctx.guild.create_role(name=f"🎨 {value} ({ctx.author.name})",
                                           color=discord.Color.random())
        await ctx.author.add_roles(role)
        await ctx.reply(f"🎨 Your color role **{value}** is live!")
        return
    await ctx.reply("Usage: `sh!buy vip` or `sh!buy color <name>`")

@bot.command()
async def rank(ctx):
    u = get_user(ctx.author.id)
    lvl = 1 + int(u["xp"] // 100)
    next_at = lvl * 100
    await ctx.reply(f"⭐ {ctx.author.mention} — Level **{lvl}** · {u['xp']} XP · {u['coins']} 🪙 "
                    f"({next_at - u['xp']} XP to next)")

@bot.command()
async def lb(ctx):
    s = _load(XP_FILE)
    top = sorted(s.items(), key=lambda kv: kv[1].get("coins", 0), reverse=True)[:5]
    lines = [f"`{i+1}.` <@{uid}> — {v.get('coins',0)} 🪙 · Lvl {1 + v.get('xp',0) // 100}"
             for i, (uid, v) in enumerate(top)]
    await ctx.reply(embed=discord.Embed(title="🏆 Leaderboard",
                                        description="\n".join(lines) or "empty", color=0x2b6e37))

# ---------------------------------------------------------------- minigames
HANG_WORDS = ["OPUS", "SPOTIFY", "PLAYLIST", "LYRICS", "SYNCED", "DOWNLOAD", "SHUNGITE", "THUMBNAIL",
              "METADATA", "GENRE", "FFMPEG", "M3U", "WAV2VEC", "WHISPERX", "PREMIUM", "SUBTITLE",
              "EMBED", "BITRATE", "CODEC", "REMASTER"]
HANG_STAGES = [
    "```\n ┌───┐\n │   │\n     │\n     │\n     │\n     │\n ═══╯```",
    "```\n ┌───┐\n │   │\n ○   │\n     │\n     │\n     │\n ═══╯```",
    "```\n ┌───┐\n │   │\n ○   │\n │   │\n     │\n     │\n ═══╯```",
    "```\n ┌───┐\n │   │\n ○   │\n ╲│  │\n     │\n     │\n ═══╯```",
    "```\n ┌───┐\n │   │\n ○   │\n ╲│╱  │\n     │\n     │\n ═══╯```",
    "```\n ┌───┐\n │   │\n ○   │\n ╲│╱  │\n ╱   │\n     │\n ═══╯```",
    "```\n ┌───┐\n │   │\n ○   │\n ╲│╱  │\n ╱╲  │\n     │\n ═══╯```",
]

@bot.command()
async def hangman(ctx):
    if ctx.channel.id in GAMES:
        await ctx.reply("A game is already running here — `sh!g <letter>`")
        return
    word = random.choice(HANG_WORDS)
    GAMES[ctx.channel.id] = {"word": word, "guessed": set(), "wrong": 0}
    await ctx.reply(f"🎬 **Hangman!** Guess with `sh!g <letter>` — {len(word)} letters, music/tech themed\n"
                    f"```\n{' '.join('·' for _ in word)}\n```")

@bot.command()
async def g(ctx, letter=None):
    game = GAMES.get(ctx.channel.id)
    if not game or not letter:
        return
    letter = letter.upper()[:1]
    if letter in game["guessed"]:
        await ctx.reply("Already guessed!")
        return
    game["guessed"].add(letter)
    if letter not in game["word"]:
        game["wrong"] += 1
    stage = HANG_STAGES[min(game["wrong"], 6)]
    shown = " ".join(c if c in game["guessed"] else "·" for c in game["word"])
    if game["wrong"] >= 6:
        GAMES.pop(ctx.channel.id, None)
        await ctx.reply(f"{stage}\n💀 Dead! The word was **{game['word']}**")
    elif all(c in game["guessed"] for c in game["word"]):
        GAMES.pop(ctx.channel.id, None)
        add_xp(ctx.author.id, 30); add_coins(ctx.author.id, 40)
        await ctx.reply(f"{stage}\n🎉 {ctx.author.mention} won! **{game['word']}** (+30 XP, +40 🪙)")
    else:
        await ctx.reply(f"{stage}\n`{shown}` — wrong {game['wrong']}/6")

@bot.command()
async def trivia(ctx):
    if ctx.channel.id in TRIVIA_ACTIVE:
        await ctx.reply("Trivia already running — `sh!a <answer>`")
        return
    q, answers = random.choice(TRIVIA)
    TRIVIA_ACTIVE[ctx.channel.id] = {"answers": answers, "expires": time.time() + 45, "q": q}
    await ctx.reply(f"🎧 **Trivia:** {q}\nAnswer with `sh!a <answer>` — 45s, 60🪙 prize!")

@bot.command()
async def a(ctx, *, answer=None):
    t = TRIVIA_ACTIVE.get(ctx.channel.id)
    if not t or not answer:
        return
    if time.time() > t["expires"]:
        TRIVIA_ACTIVE.pop(ctx.channel.id, None)
        await ctx.reply("⌛ Too slow!")
        return
    if answer.strip().lower() in t["answers"]:
        TRIVIA_ACTIVE.pop(ctx.channel.id, None)
        add_xp(ctx.author.id, 20); add_coins(ctx.author.id, 60)
        await ctx.reply(f"🎉 Correct {ctx.author.mention}! +20 XP, +60 🪙")
    else:
        await ctx.reply("❌ Nope!")

@bot.command()
async def slots(ctx):
    icons = ["🎵", "🎶", "🎸", "🥁", "🎹", "🎤"]
    cost = 10
    if get_user(ctx.author.id)["coins"] < cost:
        await ctx.reply("Need 10 🪙 to spin.")
        return
    add_coins(ctx.author.id, -cost)
    reels = [random.choice(icons) for _ in range(3)]
    if reels[0] == reels[1] == reels[2]:
        add_coins(ctx.author.id, 150)
        await ctx.reply(f"🎰 {' '.join(reels)} — **JACKPOT!** +150 🪙")
    elif reels[0] == reels[1] or reels[1] == reels[2] or reels[0] == reels[2]:
        add_coins(ctx.author.id, 25)
        await ctx.reply(f"🎰 {' '.join(reels)} — pair! +25 🪙")
    else:
        await ctx.reply(f"🎰 {' '.join(reels)} — nothing. Try again!")

@bot.command()
async def guess(ctx):
    if ctx.author.id in GUESS_ACTIVE:
        await ctx.reply("You already have a game running!")
        return
    GUESS_ACTIVE[ctx.author.id] = {"n": random.randint(1, 100), "tries": 5}
    await ctx.reply("🎲 I picked a number 1-100. You have **5 tries** — just type the number!")

@bot.command()
async def typerace(ctx):
    words = ["opus", "spotify", "playlist", "lyrics", "shungite", "download", "whisperx",
             "metadata", "thumbnail", "ffmpeg", "remaster", "subtitle", "embedded"]
    phrase = " ".join(random.sample(words, 4))
    t0 = time.time()
    await ctx.reply(f"⌨️ **Type this fast!** First correct typing wins.\n```\n{phrase}\n```")

    def check(m):
        return m.channel.id == ctx.channel.id and not m.author.bot

    try:
        m = await bot.wait_for("message", timeout=30, check=check)
        dt = time.time() - t0
        if m.content.strip().lower() == phrase:
            wpm = int(len(phrase.split()) / dt * 60)
            add_coins(m.author.id, min(100, max(10, wpm)))
            await ctx.reply(f"⚡ {m.author.mention} — {wpm} WPM! +{min(100, max(10, wpm))} 🪙")
        else:
            await ctx.reply("Typo! No prize 😢")
    except asyncio.TimeoutError:
        await ctx.reply("⌛ Nobody typed it in time!")

@bot.command()
async def roll(ctx, sides=6):
    try:
        await ctx.reply(f"🎲 {random.randint(1, int(sides))}")
    except Exception:
        await ctx.reply("Pick a number: `sh!roll 20`")

@bot.command()
async def coin(ctx):
    await ctx.reply(f"🪙 {random.choice(['Heads', 'Tails'])}")

@bot.command()
async def rps(ctx, choice=None):
    if not choice:
        await ctx.reply("Pick: `sh!rps rock` (or paper / scissors)")
        return
    cmap = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
    short = {"r": "rock", "p": "paper", "s": "scissors"}
    you = short.get(choice.lower()[0]) or (choice.lower() if choice.lower() in cmap else None)
    if not you:
        await ctx.reply("Pick: rock / paper / scissors")
        return
    pc = random.choice(list(cmap))
    if you == pc:
        await ctx.reply(f"{cmap[you]} vs {cmap[pc]} — tie!")
    elif (you, pc) in [("rock", "scissors"), ("paper", "rock"), ("scissors", "paper")]:
        add_xp(ctx.author.id, 10); add_coins(ctx.author.id, 15)
        await ctx.reply(f"{cmap[you]} vs {cmap[pc]} — **you win!** (+10 XP, +15 🪙)")
    else:
        await ctx.reply(f"{cmap[you]} vs {cmap[pc]} — you lose!")

@bot.command(name="8ball")
async def eightball_cmd(ctx, *, question=None):
    if not question:
        await ctx.reply("🥤 ask a question!")
        return
    responses = ["yes.", "no.", "absolutely.", "ask again later.", "doubt it.", "100%",
                 "the opus says yes", "the matcher rejects your question", "download it and find out",
                 "sign says: stream it locally"]
    await ctx.reply(f"🎱 {random.choice(responses)}")

# ---------------------------------------------------------------- utility
@bot.command()
async def poll(ctx, *, text=None):
    if not text or "|" not in text:
        await ctx.reply("Usage: `sh!poll question | option1 | option2`")
        return
    parts = [p.strip() for p in text.split("|")]
    if len(parts) < 3 or len(parts) > 11:
        await ctx.reply("Need 2-10 options.")
        return
    letters = ["🇦", "🇧", "🇨", "🇩", "🇪", "🇫", "🇬", "🇭", "🇮", "🇯"]
    desc = "\n".join(f"{letters[i]} {opt}" for i, opt in enumerate(parts[1:]))
    e = discord.Embed(title=f"📊 {parts[0]}", description=desc, color=0x2b6e37)
    e.set_footer(text=f"Poll by {ctx.author.display_name}")
    m = await ctx.channel.send(embed=e)
    for i in range(len(parts) - 1):
        await m.add_reaction(letters[i])

SONG_RECS = ["King Gnu - SPECIALZ", "LiSA - 炎", "Ado - うっせぇわ", "Billie Eilish - ocean eyes",
             "The Weeknd - Blinding Lights", "Neoni - MACHINE", "YUNGBLUD - Abyss",
             "Mob Choir - 99.9", "AmaLee - Again", "Jagwar Twin - great time to be human"]

@bot.command()
async def randomsong(ctx):
    await ctx.reply(f"🎵 Try: **{random.choice(SONG_RECS)}** — grab it with Shungite!")

if __name__ == "__main__":
    if not TOKEN:
        print("No token found.")
        raise SystemExit(1)
    bot.run(TOKEN)
