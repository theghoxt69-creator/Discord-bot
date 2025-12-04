# darkmc_bot.py
# Single-file Discord bot with moderation, fun, economy, welcomes, reaction roles, ticket system & Minecraft status.
# Requirements: python 3.10+
# Install: pip install -U discord.py aiohttp python-dotenv mcstatus

import os
import sqlite3
import asyncio
import random
import time
from datetime import datetime, timedelta

import aiohttp
import discord
from discord.ext import commands, tasks
from mcstatus import JavaServer  # pip install mcstatus
from dotenv import load_dotenv

# -------------------------
# CONFIG - edit as needed
# -------------------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")  # set in environment or in a .env file
PREFIX = "!"
WELCOME_CHANNEL_NAME = "welcome"
LOG_CHANNEL_NAME = "mod-logs"
AUTO_ROLE_NAME = "Member"  # give on join (if exists)
MUTE_ROLE_NAME = "Muted"
TICKET_CATEGORY_NAME = "Tickets"
DB_PATH = "bot_data.db"
OWNER_ID = None  # put your Discord user id (int) or leave None

# -------------------------
# Intents
# -------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True
intents.guilds = True
intents.messages = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

# -------------------------
# Simple DB (sqlite)
# -------------------------
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
c.execute("""CREATE TABLE IF NOT EXISTS economy (user_id INTEGER PRIMARY KEY, balance INTEGER)""")
c.execute("""CREATE TABLE IF NOT EXISTS warns (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, mod_id INTEGER, reason TEXT, time INTEGER)""")
c.execute("""CREATE TABLE IF NOT EXISTS shop (item TEXT PRIMARY KEY, price INTEGER, description TEXT)""")
c.execute("""CREATE TABLE IF NOT EXISTS reaction_roles (msg_id INTEGER, emoji TEXT, role_id INTEGER)""")
conn.commit()

# Prepopulate shop
c.execute("INSERT OR IGNORE INTO shop (item,price,description) VALUES (?,?,?)",
          ("VIP", 500, "Special VIP Role"))
conn.commit()

# -------------------------
# In-memory anti-spam
# -------------------------
message_log = {}  # user_id -> [timestamps]

# -------------------------
# Helper functions
# -------------------------
def log_channel(guild):
    return discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)

async def ensure_mute_role(guild):
    role = discord.utils.get(guild.roles, name=MUTE_ROLE_NAME)
    if not role:
        perms = discord.Permissions(send_messages=False, speak=False)
        role = await guild.create_role(name=MUTE_ROLE_NAME, permissions=perms, reason="Create mute role")
        # deny send permissions for all text channels
        for ch in guild.text_channels:
            try:
                await ch.set_permissions(role, send_messages=False, add_reactions=False)
            except Exception:
                pass
    return role

def fmt_time(ts):
    return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S UTC")

# -------------------------
# EVENTS
# -------------------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")
    bot.loop.create_task(status_task())
    # ensure DB commit loop
    periodic_commit.start()

@bot.event
async def on_member_join(member):
    g = member.guild
    # Auto role
    role = discord.utils.get(g.roles, name=AUTO_ROLE_NAME)
    try:
        if role:
            await member.add_roles(role, reason="Auto role on join")
    except Exception:
        pass
    # Welcome message
    ch = discord.utils.get(g.text_channels, name=WELCOME_CHANNEL_NAME)
    if ch:
        embed = discord.Embed(title=f"Welcome {member.display_name}!", color=0x2f3136,
                              description=f"Welcome to **{g.name}**. Read rules and have fun!")
        embed.set_thumbnail(url=member.display_avatar.url)
        await ch.send(embed=embed)
    # log
    lc = log_channel(g)
    if lc:
        await lc.send(f"🟢 {member.mention} joined at {fmt_time(time.time())}")

@bot.event
async def on_raw_reaction_add(payload):
    # reaction role handler (works for uncached messages)
    row = None
    cur = conn.cursor()
    cur.execute("SELECT role_id FROM reaction_roles WHERE msg_id = ? AND emoji = ?", (payload.message_id, str(payload.emoji)))
    row = cur.fetchone()
    if row:
        guild = bot.get_guild(payload.guild_id)
        role = guild.get_role(row[0])
        member = guild.get_member(payload.user_id)
        if role and member:
            try:
                await member.add_roles(role, reason="Reaction role add")
            except Exception:
                pass

@bot.event
async def on_raw_reaction_remove(payload):
    cur = conn.cursor()
    cur.execute("SELECT role_id FROM reaction_roles WHERE msg_id = ? AND emoji = ?", (payload.message_id, str(payload.emoji)))
    row = cur.fetchone()
    if row:
        guild = bot.get_guild(payload.guild_id)
        role = guild.get_role(row[0])
        member = guild.get_member(payload.user_id)
        if role and member:
            try:
                await member.remove_roles(role, reason="Reaction role remove")
            except Exception:
                pass

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    # simple anti-spam: >5 messages in 6 seconds => mute for 30s
    uid = message.author.id
    now = time.time()
    lst = message_log.get(uid, [])
    lst = [t for t in lst if now - t < 6]
    lst.append(now)
    message_log[uid] = lst
    if len(lst) > 5:
        # mute
        try:
            role = await ensure_mute_role(message.guild)
            await message.author.add_roles(role, reason="Auto mute for spamming")
            await message.channel.send(f"{message.author.mention} has been muted for spamming.")
            lc = log_channel(message.guild)
            if lc:
                await lc.send(f"Auto-muted {message.author} for spamming.")
            await asyncio.sleep(30)
            try:
                await message.author.remove_roles(role, reason="Auto unmute")
            except Exception:
                pass
        except Exception:
            pass
    await bot.process_commands(message)

# -------------------------
# TASKS
# -------------------------
@tasks.loop(seconds=60)
async def periodic_commit():
    conn.commit()

async def status_task():
    await bot.wait_until_ready()
    statuses = [
        f"{PREFIX}help | {len(bot.guilds)} servers",
        "DarkMC Bot — moderation & fun",
        f"{PREFIX}mcstatus <ip>",
    ]
    i = 0
    while True:
        try:
            await bot.change_presence(activity=discord.Game(statuses[i % len(statuses)]))
        except Exception:
            pass
        i += 1
        await asyncio.sleep(20)

# -------------------------
# COMMANDS - Moderation
# -------------------------
def has_mod_perms():
    async def predicate(ctx):
        return ctx.author.guild_permissions.manage_messages or (OWNER_ID and ctx.author.id == OWNER_ID)
    return commands.check(predicate)

@bot.command(name="ban")
@has_mod_perms()
async def ban(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    try:
        await member.ban(reason=reason)
        await ctx.send(f"✅ Banned {member} — {reason}")
        lc = log_channel(ctx.guild)
        if lc:
            await lc.send(f"🔨 {ctx.author} banned {member} — {reason}")
    except Exception as e:
        await ctx.send(f"Could not ban: {e}")

@bot.command(name="kick")
@has_mod_perms()
async def kick(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    try:
        await member.kick(reason=reason)
        await ctx.send(f"✅ Kicked {member} — {reason}")
        lc = log_channel(ctx.guild)
        if lc:
            await lc.send(f"👢 {ctx.author} kicked {member} — {reason}")
    except Exception as e:
        await ctx.send(f"Could not kick: {e}")

@bot.command(name="mute")
@has_mod_perms()
async def mute(ctx, member: discord.Member, seconds: int = 60, *, reason: str = "No reason"):
    role = await ensure_mute_role(ctx.guild)
    try:
        await member.add_roles(role, reason=reason)
        await ctx.send(f"🔇 Muted {member} for {seconds}s — {reason}")
        lc = log_channel(ctx.guild)
        if lc:
            await lc.send(f"🔇 {ctx.author} muted {member} for {seconds}s — {reason}")
        await asyncio.sleep(seconds)
        try:
            await member.remove_roles(role, reason="Auto unmute")
            await ctx.send(f"✅ Unmuted {member}")
        except Exception:
            pass
    except Exception as e:
        await ctx.send(f"Could not mute: {e}")

@bot.command(name="warn")
@has_mod_perms()
async def warn(ctx, member: discord.Member, *, reason: str = "No reason"):
    ts = int(time.time())
    c.execute("INSERT INTO warns (user_id, mod_id, reason, time) VALUES (?,?,?,?)",
              (member.id, ctx.author.id, reason, ts))
    conn.commit()
    await ctx.send(f"⚠️ Warned {member} — {reason}")
    lc = log_channel(ctx.guild)
    if lc:
        await lc.send(f"⚠️ {ctx.author} warned {member} — {reason} at {fmt_time(ts)}")

@bot.command(name="warnings")
async def warnings(ctx, member: discord.Member = None):
    member = member or ctx.author
    cur = conn.cursor()
    cur.execute("SELECT id, mod_id, reason, time FROM warns WHERE user_id = ?", (member.id,))
    rows = cur.fetchall()
    if not rows:
        await ctx.send(f"No warns for {member}.")
        return
    em = discord.Embed(title=f"Warnings for {member}", color=0xeb4034)
    for r in rows:
        em.add_field(name=f"ID {r[0]} by {r[1]}", value=f"{r[2]} at {fmt_time(r[3])}", inline=False)
    await ctx.send(embed=em)

@bot.command(name="unwarn")
@has_mod_perms()
async def unwarn(ctx, warn_id: int):
    cur = conn.cursor()
    cur.execute("DELETE FROM warns WHERE id = ?", (warn_id,))
    conn.commit()
    await ctx.send(f"Removed warn id {warn_id} (if existed).")

# -------------------------
# Economy commands
# -------------------------
def get_balance(user_id):
    cur = conn.cursor()
    cur.execute("SELECT balance FROM economy WHERE user_id = ?", (user_id,))
    r = cur.fetchone()
    if not r:
        cur.execute("INSERT INTO economy (user_id, balance) VALUES (?,?)", (user_id, 100))
        conn.commit()
        return 100
    return r[0]

def change_balance(user_id, amount):
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO economy (user_id,balance) VALUES (?,?)", (user_id, 0))
    cur.execute("UPDATE economy SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()

@bot.command(name="balance", aliases=["bal"])
async def balance(ctx, member: discord.Member = None):
    member = member or ctx.author
    bal = get_balance(member.id)
    await ctx.send(f"{member.mention} has 💰 {bal} coins.")

@bot.command(name="daily")
async def daily(ctx):
    uid = ctx.author.id
    # simple daily cooldown by file
    key = f"daily:{uid}"
    last_ts = bot.__dict__.get(key, 0)
    now = time.time()
    if now - last_ts < 24*3600:
        remaining = int(24*3600 - (now - last_ts))
        await ctx.send(f"You've already claimed daily. Try again in {remaining//3600}h {(remaining%3600)//60}m.")
        return
    amount = random.randint(100, 300)
    change_balance(uid, amount)
    bot.__dict__[key] = now
    await ctx.send(f"🎁 You claimed your daily {amount} coins!")

@bot.command(name="work")
async def work(ctx):
    amount = random.randint(20, 150)
    change_balance(ctx.author.id, amount)
    await ctx.send(f"💼 You worked and earned {amount} coins!")

@bot.command(name="shop")
async def shop(ctx):
    cur = conn.cursor()
    cur.execute("SELECT item,price,description FROM shop")
    rows = cur.fetchall()
    em = discord.Embed(title="Shop", description="Buy items with `!buy <item>`", color=0x00ff00)
    for r in rows:
        em.add_field(name=f"{r[0]} — {r[1]} coins", value=r[2], inline=False)
    await ctx.send(embed=em)

@bot.command(name="buy")
async def buy(ctx, item: str):
    item = item.strip()
    cur = conn.cursor()
    cur.execute("SELECT price FROM shop WHERE item = ?", (item,))
    r = cur.fetchone()
    if not r:
        await ctx.send("No such item.")
        return
    price = r[0]
    bal = get_balance(ctx.author.id)
    if bal < price:
        await ctx.send("Not enough coins.")
        return
    change_balance(ctx.author.id, -price)
    await ctx.send(f"Purchased {item} for {price} coins.")
    # example: give role if item is role name
    role = discord.utils.get(ctx.guild.roles, name=item)
    if role:
        try:
            await ctx.author.add_roles(role)
            await ctx.send(f"Given role {role.name}.")
        except Exception:
            pass

# -------------------------
# Fun / Utility
# -------------------------
@bot.command(name="meme")
async def meme(ctx):
    # fetch top meme from r/memes
    url = "https://www.reddit.com/r/memes/top.json?limit=50&t=day"
    headers = {"User-Agent": "DiscordBot-Memes/0.1"}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers) as r:
                data = await r.json()
                posts = data["data"]["children"]
                post = random.choice(posts)["data"]
                title = post["title"]
                img = post.get("url_overridden_by_dest", None)
                em = discord.Embed(title=title)
                if img and (img.endswith(".jpg") or img.endswith(".png") or img.endswith(".gif") or "i.redd.it" in img):
                    em.set_image(url=img)
                await ctx.send(embed=em)
        except Exception as e:
            await ctx.send("Could not fetch a meme right now.")

@bot.command(name="avatar")
async def avatar(ctx, member: discord.Member = None):
    member = member or ctx.author
    em = discord.Embed(title=f"{member}'s avatar")
    em.set_image(url=member.display_avatar.url)
    await ctx.send(embed=em)

@bot.command(name="say")
async def say(ctx, *, message: str):
    await ctx.message.delete()
    await ctx.send(message)

@bot.command(name="serverinfo")
async def serverinfo(ctx):
    g = ctx.guild
    em = discord.Embed(title=g.name, description=f"ID: {g.id}")
    em.add_field(name="Members", value=str(g.member_count))
    em.add_field(name="Channels", value=str(len(g.channels)))
    em.set_thumbnail(url=g.icon.url if g.icon else discord.Embed.Empty)
    await ctx.send(embed=em)

@bot.command(name="userinfo")
async def userinfo(ctx, member: discord.Member = None):
    member = member or ctx.author
    em = discord.Embed(title=str(member), color=0x00aaee)
    em.add_field(name="ID", value=member.id)
    em.add_field(name="Joined", value=member.joined_at.strftime("%Y-%m-%d") if member.joined_at else "Unknown")
    em.set_thumbnail(url=member.display_avatar.url)
    await ctx.send(embed=em)

# -------------------------
# Reaction role management
# -------------------------
@bot.command(name="reaction_role_add")
@has_mod_perms()
async def reaction_role_add(ctx, message_id: int, emoji: str, role: discord.Role):
    try:
        c.execute("INSERT INTO reaction_roles (msg_id, emoji, role_id) VALUES (?,?,?)", (message_id, emoji, role.id))
        conn.commit()
        await ctx.send(f"Added reaction role: `{emoji}` -> {role.name} for message `{message_id}`")
    except Exception as e:
        await ctx.send(f"Error: {e}")

@bot.command(name="reaction_role_remove")
@has_mod_perms()
async def reaction_role_remove(ctx, message_id: int, emoji: str):
    c.execute("DELETE FROM reaction_roles WHERE msg_id = ? AND emoji = ?", (message_id, emoji))
    conn.commit()
    await ctx.send("Removed reaction role (if existed).")

# -------------------------
# Ticket system
# -------------------------
@bot.command(name="ticket")
async def ticket(ctx, *, reason: str = None):
    guild = ctx.guild
    cat = discord.utils.get(guild.categories, name=TICKET_CATEGORY_NAME)
    if not cat:
        cat = await guild.create_category(TICKET_CATEGORY_NAME)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        ctx.author: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
    }
    channel = await guild.create_text_channel(f"ticket-{ctx.author.name}-{ctx.author.discriminator}", category=cat, overwrites=overwrites)
    await channel.send(f"Ticket created by {ctx.author.mention}\nReason: {reason}")
    await ctx.send(f"Your ticket has been created: {channel.mention}")

# -------------------------
# Minecraft server query
# -------------------------
@bot.command(name="mcstatus")
async def mcstatus(ctx, host: str, port: int = 25565):
    await ctx.send("Querying server...")
    try:
        server = JavaServer(host, port)
        status = server.status()
        players = status.players.online
        motd = status.description if hasattr(status, "description") else "N/A"
        em = discord.Embed(title=f"Minecraft Server {host}:{port}",
                           description=str(motd),
                           color=0x55ff55)
        em.add_field(name="Version", value=status.version.name)
        em.add_field(name="Players", value=str(players))
        await ctx.send(embed=em)
    except Exception as e:
        await ctx.send(f"Could not query server: {e}")

# -------------------------
# Admin helpers
# -------------------------
@bot.command(name="setlog")
@has_mod_perms()
async def setlog(ctx, channel: discord.TextChannel):
    # just create a channel with name LOG_CHANNEL_NAME or rename provided
    await channel.edit(name=LOG_CHANNEL_NAME)
    await ctx.send(f"Set log channel: {channel.mention}")

@bot.command(name="help")
async def help_cmd(ctx):
    em = discord.Embed(title="Bot Help", description=f"Prefix: `{PREFIX}`", color=0x7289da)
    em.add_field(name="Moderation", value="`ban/kick/mute/warn/warnings/unwarn`", inline=False)
    em.add_field(name="Economy", value="`balance/daily/work/shop/buy`", inline=False)
    em.add_field(name="Fun", value="`meme/avatar/say`", inline=False)
    em.add_field(name="Utility", value="`serverinfo/userinfo/mcstatus/ticket`", inline=False)
    em.add_field(name="Reaction Roles", value="`reaction_role_add/remove` (mod only)", inline=False)
    await ctx.send(embed=em)

# -------------------------
# Run
# -------------------------
if not TOKEN:
    print("ERROR: DISCORD_TOKEN not found in environment.")
else:
    bot.run(TOKEN)