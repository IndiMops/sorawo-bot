import asyncio
import functools
import itertools
import math
import random
import sqlite3
import string
import json
import img_link


import discord
import youtube_dl
import animec
from asyncio import sleep
from async_timeout import timeout
from discord.ext import commands
from discord_components import DiscordComponents, Button, ButtonStyle


# Silence useless bug reports messages
youtube_dl.utils.bug_reports_message = lambda: ''


class VoiceError(Exception):
    pass


class YTDLError(Exception):
    pass


class YTDLSource(discord.PCMVolumeTransformer):
    YTDL_OPTIONS = {
        'format': 'bestaudio/best',
        'extractaudio': True,
        'audioformat': 'mp3',
        'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
        'restrictfilenames': True,
        'noplaylist': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'auto',
        'source_address': '0.0.0.0',
    }

    FFMPEG_OPTIONS = {
        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
        'options': '-vn',
    }

    ytdl = youtube_dl.YoutubeDL(YTDL_OPTIONS)

    def __init__(self, ctx: commands.Context, source: discord.FFmpegPCMAudio, *, data: dict, volume: float = 0.5):
        super().__init__(source, volume)

        self.requester = ctx.author
        self.channel = ctx.channel
        self.data = data

        self.uploader = data.get('uploader')
        self.uploader_url = data.get('uploader_url')
        date = data.get('upload_date')
        self.upload_date = date[6:8] + '.' + date[4:6] + '.' + date[0:4]
        self.title = data.get('title')
        self.thumbnail = data.get('thumbnail')
        self.description = data.get('description')
        self.duration = self.parse_duration(int(data.get('duration')))
        self.tags = data.get('tags')
        self.url = data.get('webpage_url')
        self.views = data.get('view_count')
        self.likes = data.get('like_count')
        self.dislikes = data.get('dislike_count')
        self.stream_url = data.get('url')

    def __str__(self):
        return '**{0.title}** by **{0.uploader}**'.format(self)

    @classmethod
    async def create_source(cls, ctx: commands.Context, search: str, *, loop: asyncio.BaseEventLoop = None):
        loop = loop or asyncio.get_event_loop()

        partial = functools.partial(cls.ytdl.extract_info, search, download=False, process=False)
        data = await loop.run_in_executor(None, partial)

        if data is None:
            raise YTDLError('Couldn\'t find anything that matches `{}`'.format(search))

        if 'entries' not in data:
            process_info = data
        else:
            process_info = None
            for entry in data['entries']:
                if entry:
                    process_info = entry
                    break

            if process_info is None:
                raise YTDLError('Couldn\'t find anything that matches `{}`'.format(search))

        webpage_url = process_info['webpage_url']
        partial = functools.partial(cls.ytdl.extract_info, webpage_url, download=False)
        processed_info = await loop.run_in_executor(None, partial)

        if processed_info is None:
            raise YTDLError('Couldn\'t fetch `{}`'.format(webpage_url))

        if 'entries' not in processed_info:
            info = processed_info
        else:
            info = None
            while info is None:
                try:
                    info = processed_info['entries'].pop(0)
                except IndexError:
                    raise YTDLError('Couldn\'t retrieve any matches for `{}`'.format(webpage_url))

        return cls(ctx, discord.FFmpegPCMAudio(info['url'], **cls.FFMPEG_OPTIONS), data=info)

    @staticmethod
    def parse_duration(duration: int):
        minutes, seconds = divmod(duration, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)

        duration = []
        if days > 0:
            duration.append('{} дній'.format(days))
        if hours > 0:
            duration.append('{} годин'.format(hours))
        if minutes > 0:
            duration.append('{} хвиллин'.format(minutes))
        if seconds > 0:
            duration.append('{} секунд'.format(seconds))

        return ', '.join(duration)


class Song:
    __slots__ = ('source', 'requester')

    def __init__(self, source: YTDLSource):
        self.source = source
        self.requester = source.requester

    def create_embed(self):
        embed = (discord.Embed(description='```css\n{0.source.title}\n```'.format(self),
                               color=0x22ff00)
                 .add_field(name='Тривалість треку', value=self.source.duration)
                 .add_field(name='Запросив', value=self.requester.mention)
                 .add_field(name='Автор відео', value='[{0.source.uploader}]({0.source.uploader_url})'.format(self))
                 .add_field(name='Поислання', value='[Посилання]({0.source.url})'.format(self))
                 .set_image(url=self.source.thumbnail))

        return embed


class SongQueue(asyncio.Queue):
    def __getitem__(self, item):
        if isinstance(item, slice):
            return list(itertools.islice(self._queue, item.start, item.stop, item.step))
        else:
            return self._queue[item]

    def __iter__(self):
        return self._queue.__iter__()

    def __len__(self):
        return self.qsize()

    def clear(self):
        self._queue.clear()

    def shuffle(self):
        random.shuffle(self._queue)

    def remove(self, index: int):
        del self._queue[index]


class VoiceState:
    def __init__(self, bot: commands.Bot, ctx: commands.Context):
        self.bot = bot
        self._ctx = ctx

        self.current = None
        self.voice = None
        self.next = asyncio.Event()
        self.songs = SongQueue()

        self._loop = False
        self._volume = 0.5
        self.skip_votes = set()

        self.audio_player = bot.loop.create_task(self.audio_player_task())

    def __del__(self):
        self.audio_player.cancel()

    @property
    def loop(self):
        return self._loop

    @loop.setter
    def loop(self, value: bool):
        self._loop = value

    @property
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, value: float):
        self._volume = value

    @property
    def is_playing(self):
        return self.voice and self.current

    async def audio_player_task(self):
        while True:
            self.next.clear()

            if not self.loop:
                # Try to get the next song within 3 minutes.
                # If no song will be added to the queue in time,
                # the player will disconnect due to performance
                # reasons.
                try:
                    async with timeout(180):  # 3 minutes
                        self.current = await self.songs.get()
                except asyncio.TimeoutError:
                    self.bot.loop.create_task(self.stop())
                    return

            self.current.source.volume = self._volume
            self.voice.play(self.current.source, after=self.play_next_song)
            await self.current.source.channel.send(embed=self.current.create_embed())

            await self.next.wait()

    def play_next_song(self, error=None):
        

        self.next.set()

    def skip(self):
        self.skip_votes.clear()

        if self.is_playing:
            self.voice.stop()

    async def stop(self):
        self.songs.clear()

        if self.voice:
            await self.voice.disconnect()
            self.voice = None


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_states = {}

    def get_voice_state(self, ctx: commands.Context):
        state = self.voice_states.get(ctx.guild.id)
        if not state:
            state = VoiceState(self.bot, ctx)
            self.voice_states[ctx.guild.id] = state

        return state

    def cog_unload(self):
        for state in self.voice_states.values():
            self.bot.loop.create_task(state.stop())

    def cog_check(self, ctx: commands.Context):
        if not ctx.guild:
            raise commands.NoPrivateMessage('Ця команда не використовується в ПП (Приватні повідомлення)')

        return True

    async def cog_before_invoke(self, ctx: commands.Context):
        ctx.voice_state = self.get_voice_state(ctx)

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        await ctx.send(embed = discord.Embed(color = 0xff0000, description = 'Відбулася якась помилка: {}'.format(str(error))))

    @commands.command(name='join', invoke_without_subcommand=True)
    async def _join(self, ctx: commands.Context):
        """Приєднується до голосового каналу."""

        destination = ctx.author.voice.channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return

        ctx.voice_state.voice = await destination.connect()

    @commands.command(name='summon')
    @commands.has_permissions(manage_guild=True)
    async def _summon(self, ctx: commands.Context, *, channel: discord.VoiceChannel = None):
        """Summons the bot to a voice channel.
        If no channel was specified, it joins your channel.
        """

        if not channel and not ctx.author.voice:
            raise VoiceError('Ви не підключені до голосового каналу. І не вказали, куди підключатися.')

        destination = channel or ctx.author.voice.channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return

        ctx.voice_state.voice = await destination.connect()

    @commands.command(name = 'leave', aliases=['disconnect'])
    @commands.has_permissions(manage_guild=True)
    async def _leave(self, ctx: commands.Context):
        """Очищає чергу та залишає голосовий канал."""

        if not ctx.voice_state.voice:
            return await ctx.send(embed = discord.Embed(color = 0xff0000, description = 'Бот не в голосовому каналі. Навіщо його вигянати?'))

        await ctx.voice_state.stop()
        del self.voice_states[ctx.guild.id]

    @commands.command(name = 'volume', aliases=['vol'])
    async def _volume(self, ctx: commands.Context, *, volume: int):
        """Встановлює гучність програвача."""

        if not ctx.voice_state.is_playing:
            return await ctx.send(embed = discord.Embed(color = 0xff0000, description = 'Зараз музика не грає. Можете увімкнути.'))

        if 0 > volume > 100:
            return await ctx.send('Гучність має бути від 0 до 100')

        ctx.voice_state.volume = volume / 100
        await ctx.send('Гучність змінена на {}%'.format(volume))

    @commands.command(name = 'now', aliases=['current', 'playing'])
    async def _now(self, ctx: commands.Context):
        """Відображає пісню, яка зараз відтворюється."""

        await ctx.send(embed = ctx.voice_state.current.create_embed())

    @commands.command(name = 'pause')
    @commands.has_permissions(manage_guild = True)
    async def _pause(self, ctx: commands.Context):
        """Pauses the currently playing song."""

        if ctx.voice_state.voice.is_playing():
            ctx.voice_state.voice.pause()
            await ctx.message.add_reaction('⏯')

    @commands.command(name='resume')
    @commands.has_permissions(manage_guild=True)
    async def _resume(self, ctx: commands.Context):
        """Resumes a currently paused song."""

        if ctx.voice_state.voice.is_paused():
            ctx.voice_state.voice.resume()
            await ctx.message.add_reaction('⏯')

    @commands.command(name='stop')
    @commands.has_permissions(manage_guild=True)
    async def _stop(self, ctx: commands.Context):
        """Stops playing song and clears the queue."""

        ctx.voice_state.songs.clear()

        if ctx.voice_state.is_playing:
            ctx.voice_state.voice.stop()
            await ctx.message.add_reaction('⏹')

    @commands.command(name='skip')
    async def _skip(self, ctx: commands.Context):
        """Vote to skip a song. The requester can automatically skip.
        3 skip votes are needed for the song to be skipped.
        """

        if not ctx.voice_state.is_playing:
            return await ctx.send(embed = discord.Embed(color = 0xff0000, description = 'Зараз музика не грає, навіщо її пропускати? Можете увімкнути.'))

        voter = ctx.message.author
        if voter == ctx.voice_state.current.requester:
            await ctx.message.add_reaction('⏭')
            ctx.voice_state.skip()

        elif voter.id not in ctx.voice_state.skip_votes:
            ctx.voice_state.skip_votes.add(voter.id)
            total_votes = len(ctx.voice_state.skip_votes)

            if total_votes >= 3:
                await ctx.message.add_reaction('⏭')
                ctx.voice_state.skip()
            else:
                await ctx.send(embed = discord.Embed(color = 0x22ff00, description = 'Голосування за перепустку додано. Проголосували: **{}/3**'.format(total_votes)))

        else:
            await ctx.send(embed = discord.Embed(color = 0xff0000, description = 'Ви вже голосували за перепустку цього треку.'))

    @commands.command(name='queue')
    async def _queue(self, ctx: commands.Context, *, page: int = 1):
        """Shows the player's queue.
        You can optionally specify the page to show. Each page contains 10 elements.
        """

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send(embed = discord.Embed(color = 0xff0000, description = 'У черзі немає треків. Можете додати.'))

        items_per_page = 10
        pages = math.ceil(len(ctx.voice_state.songs) / items_per_page)

        start = (page - 1) * items_per_page
        end = start + items_per_page

        queue = ''
        for i, song in enumerate(ctx.voice_state.songs[start:end], start=start):
            queue += '`{0}.` [**{1.source.title}**]({1.source.url})\n'.format(i + 1, song)

        embed = (discord.Embed(description='**{} пісень:**\n\n{}'.format(len(ctx.voice_state.songs), queue))
                 .set_footer(text='Сторінок {}/{}'.format(page, pages)))
        await ctx.send(embed=embed)

    @commands.command(name='shuffle')
    async def _shuffle(self, ctx: commands.Context):
        """Перемішує чергу."""

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send(embed = discord.Embed(color = 0xff0000, description = 'У черзі немає треків. Можете додати.'))

        ctx.voice_state.songs.shuffle()
        await ctx.message.add_reaction('✅')

    @commands.command(name='remove')
    async def _remove(self, ctx: commands.Context, index: int):
        """Removes a song from the queue at a given index."""

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send(embed = discord.Embed(color = 0xff0000, description = 'У черзі немає пісень. Можете додати.'))

        ctx.voice_state.songs.remove(index - 1)
        await ctx.message.add_reaction('✅')

    @commands.command(name='loop')
    async def _loop(self, ctx: commands.Context):
        """Loops the currently playing song.
        Invoke this command again to unloop the song.
        """

        if not ctx.voice_state.is_playing:
            return await ctx.send(embed = discord.Embed(color = 0xff0000, description = 'Зараз нічого не грає.'))

        # Inverse boolean value to loop and unloop.
        ctx.voice_state.loop = not ctx.voice_state.loop
        await ctx.message.add_reaction('✅')

    @commands.command(name='play')
    async def _play(self, ctx: commands.Context, *, search: str):
        """Plays a song.
        If there are songs in the queue, this will be queued until the
        other songs finished playing.
        This command automatically searches from various sites if no URL is provided.
        A list of these sites can be found here: https://rg3.github.io/youtube-dl/supportedsites.html
        """

        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        async with ctx.typing():
            try:
                source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop)
            except YTDLError as e:
                await ctx.send(embed = discord.Embed(color = 0xff0000, description = 'Виникла помилка при обробці цього запиту: {}'.format(str(e))))
            else:
                song = Song(source)

                await ctx.voice_state.songs.put(song)
                await ctx.send(embed = discord.Embed(color = 0x22ff00, description = 'Успішно додано {}'.format(str(source))))

    @_join.before_invoke
    @_play.before_invoke
    async def ensure_voice_state(self, ctx: commands.Context):
        if not ctx.author.voice or not ctx.author.voice.channel:
            raise commands.CommandError('Спершу підключись до голосового каналу.')

        if ctx.voice_client:
            if ctx.voice_client.channel != ctx.author.voice.channel:
                raise commands.CommandError('Бот вже підключився до голосового каналу.')



bot = commands.Bot('+', intents = discord.Intents.all())
bot.add_cog(Music(bot))
bot.remove_command("help")

global server_id
server_id = [927167461198016513, 930193654629412934]

@bot.event
async def on_ready():
    print('Бот готовий!')
    DiscordComponents(bot)
    global version_bot
    version_bot = str("0.3.4")
    #підключаємо базу даних
    global base, cur
    base = sqlite3.connect('datebase.db')
    cur = base.cursor()
    if base:
        print('База даних підключилась!')
    while True:
        await bot.change_presence(status=discord.Status.online, activity=discord.Game(f"+help | v{version_bot}"))
        await sleep(35)
        await bot.change_presence(status=discord.Status.online, activity=discord.Game("Бот в розробці!"))
        await sleep(35)

@bot.event
async def on_guild_join(guild):
    emb = discord.Embed(
        color = 0x22ff00,
        title = f"Всім привітики! Дякую, що запросили мене на сервер {guild.name}",
        description = f"Мене звати **Sorao**, я україномовний бот. Мій творець говорить, що я поки розробляюсь і зараз на стадії {version_bot} версії розробки. Тому я можу інколи працювати не так як би хотілось. Тому, якщо знайдете котрусь помилку переходьте до нашого [сервера підтримки](https://discord.gg/q9MVVMAu9A)."
        )
    emb.add_field(name = "Що я можу?", value = "Я простенький бот для модерації, також у мене трохи коман-утиліт які допоможуть вам із сервером, або розвеселять учасників.", inline = False)
    emb.add_field(name = "Основні мої команди", value = "У всіх ботів є свій префік і не виключення у мене він теж є, я використовую `+`, якщо ти будеш відправляти повідомлення без префікса то  я не зрозумію, що ти звертаєшся до мене.\nСписок всіх моїх команд: **+help**\nІнформація про мене: **+sorao**\nЯкщо хочеш запросити мене на свій сервер просто нажми ось [тут](https://discord.com/api/oauth2/authorize?client_id=927164571138015232&permissions=8&scope=bot)", inline = False)
    emb.set_footer(text="Думаю, що вибрали мене. Я допоможу у вашій реалізації!")
    await guild.text_channels[0].send(embed = emb)
    server_id.append(guild.id)

global footer_swich
footer_swich = 0


#команди

#увімкнути/микнути команди  футер в ембедах
@bot.command()
@commands.has_permissions(administrator = True)
async def fswitch(ctx, f_switch):
    global footer_swich
    global server_id
    if f_switch == str("вкл"):
        footer_swich = 1
        await ctx.send(embed = discord.Embed(color = 0x22ff00, description = "Футери у повідомленнях були увімкнені!"))
    elif f_switch == str("вимк"):
        footer_swich = 0
        await ctx.send(embed = discord.Embed(color = 0x22ff00, description = "Футери у повідомленнях були вимкнені!"))
@fswitch.error
async def fswitch(ctx, error):
    if isinstance(error, commands.BadArgument):
        await ctx.send(embed = discord.Embed(color = 0x22ff00, description = "Вибач, але ввів не правельні аргументи! Спробуй так: `+sfooter вкл/вимк`\nПропиши `+help fswitch`"))
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send(embed = discord.Embed(color = 0xff0000, description = "Вибач, але у тебе немає права використовувати цю команду!)"))


#очисти чат
@bot.command()
async def clear(ctx, amount = 0):
    user = ctx.author
    roles = user.roles
    this_server_id = 1
    for x in server_id:
        if x == ctx.guild.id:
            this_server_id = 1
    for role in roles:
        if role.permissions.administrator:
            if amount == 0 or None:
                nwork = discord.Embed(color = 0x22ff00, description = "Ти не вів сскільки потрібно видалити повідомлень\nСпробуйте так `+clear 5`")
                await ctx.send(embed = nwork)
            elif amount > 1:
                await ctx.channel.purge(limit = amount + 1)
                work = discord.Embed(color = 0x22ff00, title = "Готово!", description = f"Було видалено {amount} повідомлень")
                if footer_swich == 1 and this_server_id == 1:
                    work.set_footer(text=f"Команду запросив {ctx.author}", icon_url=ctx.author.avatar_url)
                await ctx.send(embed = work)
            else:
                author = ctx.message.author
                await ctx.send(f"{author.mention}, щось пішло не так!")
    if role.permissions.administrator == False:
        notperms = discord.Embed(color = 0xff0037, description = "Вибач, але в тебе не має права використовувати цю команду!")
        await ctx.send(embed = notperms)


#інформація про користувача
@bot.command()
async def uinfo(ctx, *, member: discord.Member = None, amount = 1):
    await ctx.channel.purge(limit = amount)
    if member is None:
        member = ctx.author
    this_server_id = None
    for x in server_id:
        if ctx.guild.id == x:
            this_server_id = 1
    embed = discord.Embed(color=0x22ff00)
    name = member.name
    nick = member.nick
    discriminator = member.discriminator
    sign_up = member.joined_at
    embed.add_field(name = "Ім'я", value = f"**{name}**{discriminator}", inline = True)
    embed.add_field(name = "Нік", value = nick, inline = True)
    embed.add_field(name = "Id", value = member.id, inline = True)
    embed.add_field(name = "Приєднався до сервера", value = sign_up.strftime("%d.%m.%y %H:%M"), inline = True)
    embed.add_field(name = "Зареєструвався у Discord", value = member.created_at.strftime("%d.%m.%y %H:%M "), inline = True)
    if len(member.roles) > 1: #якщо є ролі, не рахуючи @everyone
        role_string = ' '.join([r.mention for r in member.roles][1:])
        embed.add_field(name = "Ролі ({})".format(len(member.roles)-1), value = role_string, inline = True)
    else:
        embed.add_field(name="Ролі:", value="немає", inline = True)
    #футер
    embed.set_thumbnail(url=member.avatar_url)
    if footer_swich == 1 and this_server_id == 1:
        embed.set_footer(text=f"Команду запросив {ctx.message.author}", icon_url=ctx.author.avatar_url)
    await ctx.send(embed=embed)


#рамдомний мем
@bot.command()
async def memes(ctx, amount = 1):
    await ctx.channel.purge(limit = amount)
    this_server_id = None
    for x in server_id:
        if ctx.guild.id == x:
            this_server_id = 1
    embed = discord.Embed(
        color = 0x22ff00,
        title = "Випадковий мем")
    ram = random.randint(1, 1000)
    embed.set_image(url=f'https://ctk-api.herokuapp.com/meme/{ram}')
    #футер
    if footer_swich == 1 and this_server_id == 1:
        embed.set_footer(text=f"Команду запросив {ctx.author}", icon_url=ctx.author.avatar_url)
    await ctx.send(embed=embed)


#інформація про сервер
@bot.command()
async def sinfo(ctx, amount = 1):
    await ctx.channel.purge(limit = amount)
    this_server_id = None
    for x in server_id:
        if ctx.guild.id == x:
            this_server_id = 1
    server_name = ctx.guild.name
    sdesc = ctx.guild.description
    if sdesc == None:
        sdesc = "Немає опису сервера"
    sowner = ctx.guild.owner
    esowner = str("<:Sorao_yellow_server_owner:927693088124711003>")
    sid = ctx.guild.id
    sverification = str(ctx.guild.verification_level)
    if sverification == "extreme":
        sverification = "Найвищий"
    elif sverification == "high":
        sverification = "Високий"
    elif sverification == "medium":
        sverification = "Середній"
    elif sverification == "low":
        sverification = "Низький"
    elif sverification == "none":
        sverification = "Не встановлений"
    else:
        sverification = "Не знайдено"

    ilink = await ctx.channel.create_invite(max_age = 0, max_uses = 0)
    screate = ctx.guild.created_at

    schannel_rules = str(ctx.guild.rules_channel)
    if schannel_rules == "None":
        schannel_rules = "Немає"
    else:
        schannel_rules = f"<#{ctx.guild.rules_channel.id}>"

    snsfwlvl = str(ctx.guild.explicit_content_filter)
    if snsfwlvl == "all_members":
        snsfwlvl = "Перевіряти кожного учасника"
    elif snsfwlvl == "no_role":
        snsfwlvl = "Перевіряти учасників без ролей"
    elif snsfwlvl == "disabled":
        snsfwlvl = "Не встановлено"
    else:
        snsfwlvl = "Не знайдено"

    #квласні емоджі
    eallmembers = str("<:Sorao_total_members:927707095896317964>")
    emember = str("<:Sorao_member:927709667973562488>")
    ebot = str("<:Sorao_bot:927714263097827338>")
    eonline = str("<:Sorao_online:927714318848516197>")
    eidle = str("<:Sorao_idle:927714375639392317>")
    ednd = str("<:Sorao_dnd:927719774954340472>")
    eoffline = str("<:Sorao_offline:927714442156847186>")
    echannel_total = str("<:Sorao_channel_total:927929746011095051>")
    etext_channels = str("<:Sorao_text_channels:927936980300492842> ")
    evoice_channels = str("<:Sorao_voice_channels:927936980539555871>")
    estage_channels = str("<:Sorao_stage_channels:927936980325654571>")
    #підраховуємо учасників(всього, онлайн і т.д.)
    total = len(ctx.guild.members)
    online = len(list(filter(lambda m: str(m.status) == "online", ctx.guild.members)))
    idle = len(list(filter(lambda m: str(m.status) == "idle", ctx.guild.members)))
    dnd = len(list(filter(lambda m: str(m.status) == "dnd", ctx.guild.members)))
    offline = len(list(filter(lambda m: str(m.status) == "offline", ctx.guild.members)))
    humans = len(list(filter(lambda m: not m.bot, ctx.guild.members)))
    bots = len(list(filter(lambda m: m.bot, ctx.guild.members)))

    text_channels = len(ctx.guild.text_channels)
    voice_channels = len(ctx.guild.voice_channels)
    stage_channels = len(ctx.guild.stage_channels)
    total_channels = text_channels + voice_channels + stage_channels

    embed = discord.Embed(
        color = 0x22ff00,
        title = f"Інформація про сервер {server_name}",
        description = f"**Опис сервера:**\n{sdesc}",
        url = ilink
    )
    embed.add_field(name = f"{esowner}Власник сервера", value = f"{sowner.mention}", inline = True)
    embed.add_field(name = "Id", value = f"{sid}", inline = True)
    embed.add_field(name = "Створений: ", value = screate.strftime("%d.%m.%y %H:%M:%S"), inline = True)
    embed.add_field(name = "Канал з правилами/інформацією:", value = schannel_rules, inline = True)
    embed.add_field(name = "Рівень модерації:", value = f"{sverification}", inline = True)
    embed.add_field(name = "Рівень NSFW: ", value = snsfwlvl, inline = True)
    embed.add_field(name = "Учасники:", value = f"{eallmembers}Всього: **{total}**\n{emember}Учасників: **{humans}**\n{ebot}Ботів: **{bots}**", inline = True)
    embed.add_field(name = "Статуси:", value = f"{eonline}Онлайн: **{online}**\n{eidle}Відійшли: **{idle}**\n{ednd}Зайняті: **{dnd}**\n{eoffline}Не в мережэі: **{offline}**", inline = True)
    embed.add_field(name = "Канали:", value = f"{echannel_total}Всього: **{total_channels}**\n{etext_channels}Текстові: **{text_channels}**\n{evoice_channels}Голосові: **{voice_channels}**\n{estage_channels}Трибуни: **{stage_channels}**")
    embed.set_thumbnail(url = ctx.guild.icon_url)
    author = ctx.message.author
    if footer_swich == 1 and this_server_id == 1:
        embed.set_footer(text=f"Команду запросив {author.name}", icon_url=author.avatar_url)
    await ctx.send(embed = embed)



#цензура
"""
@bot.event
async def on_message(message):
    if {i.lower().translate(str.maketrans('','', string.punctuation)) for i in message.content.split(' ')}\
        .intersection(set(json.load(open('cenz.json')))) != set():
        await message.channel.send(f'{message.author.mention}, в нас заборонено використовувати ці слова!')
        await message.delete()

    await bot.process_commands(message)
ccount=+1
"""


#інформація про бота
@bot.command()
async def info(ctx):
    await ctx.channel.purge(limit = 1)
    this_server_id = None
    for x in server_id:
        if ctx.guild.id == x:
            this_server_id = 1
    Squints = str("<a:SoraSquints:931922199403696188>")
    SoraoDev = str("<a:SoraoDev:931927261731516477>")
    infoBot = discord.Embed(color = 0x22ff00, 
        title = "Інформація про мене", 
        description = f"Привіт! Мене звати Sorao! Я невеличкий Discord бот з кучею всіляких команд, які допомжуть вам розкрити ваш серевер на максимум!\n  \nПерегляь команду `+help`, щоб дызнатися про додаткову інформацію про мої функції{Squints}")
    
    infoBot.add_field(name = "Збірка:", value = f"{str(version_bot)}(15.01.2022)", inline = True)
    infoBot.add_field(name = "Розробник: ", value = f"{SoraoDev}INDMops#2404", inline = True)
    infoBot.add_field(name = "Мова:", value = "Discord.Py v1.7.3", inline = True)
    infoBot.set_thumbnail(url = "https://media.discordapp.net/attachments/930193655183056939/931940410631258142/cover.jpg")
    infoBot.set_footer(text=f"Mops Storage © 2220-2222. Усі права захищені • https://sorao-bot.tk/", icon_url = "https://media.discordapp.net/attachments/930193655183056939/931940697236443156/com-gif-maker-2--unscreen.gif")
    await ctx.send(embed = infoBot, components = [[
            Button(style = ButtonStyle.URL, label = "Сайт", url = "https://sorao-bot.tk"),
            Button(style = ButtonStyle.URL, label = "Запросити бота", url = "https://discord.com/api/oauth2/authorize?client_id=927164571138015232&permissions=8&scope=bot"),
            Button(style = ButtonStyle.URL, label = "Top.gg", url = "https://top.gg/bot/927164571138015232"),
            Button(style = ButtonStyle.URL, label = "Підтримка", url = "https://discord.gg/tb6dYwRECM")
            ]])
    await bot.wait_for("button_click")


#вигнати учасника з сервера
@bot.command()
@commands.has_permissions(administrator = True)
async def kick(ctx, member: discord.Member = None, reason = None):
    await ctx.channel.purge(limit = 1)
    this_server_id = None
    for x in server_id:
        if ctx.guild.id == x:
            this_server_id = 1
    author = ctx.author
    embed_no_i = discord.Embed(color = 0xff0000, description = "Вибач, але ти не можеш вигнати самого себе із сервера.\nПопробуй так: `+kick @sorao`")
    embed_no_arg = discord.Embed(color = 0xff0000, description = "Вибач, але ти забув згадати користувача якого ти хочеш вигнати.\nСпробуй так:`+kick @sorao`")
    if member == author:
        await ctx.send(embed = embed_no_i)
    elif member == None:
        await ctx.send(embed = embed_no_arg)
    await member.kick(reason = reason)
    embed = discord.Embed(color = 0x22ff00, description = f"{member.mention} був вигнаний із сервера користувачем {author.mention}")
    if footer_swich == 1 and this_server_id == 1:
        embed.set_footer(text=f"Команду запросив {author.name}", icon_url=author.avatar_url)
    await ctx.send(embed = embed)
@kick.error#вивід помилок для команди kick
async def kick_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(embed = discord.Embed(color = 0xff0000, description = "Вибач, але в тебе не має прав на використання цієї команди"))
    elif isinstance(error, commands.BadArgument):
        await ctx.send(embed = discord.Embed(color = 0xff0000, description = "Вибач, але ти забува ввести кого потрібно вигнати!"))


#блокувати учасника на сервері
@bot.command()
async def ban(ctx, member: discord.Member = None, *, reason = None):
    await ctx.channel.purge(limit = 1)
    author = ctx.author
    roles = author.roles
    for role in roles:
        if role.permissions.administrator == True or role.permissions.ban_members == True:
            if member == None:
                await ctx.send(embed = discord.Embed(color = 0xff0000, description = "Вибач, але ти забув згадати кого потрібно забанити. Спробуй так: `+ban @sorao`\nПропиши `+help ban` для більшої інформації."), delete_after = 30)
            elif member == author:
                await ctx.send(embed = discord.Embed(color = 0xff0000, description = "Вибач, але самого себе, не можна забанити. Спробуй так: `+ban @sorao`\nПропиши `+help ban` для більшої інформації."))
            else:
                if reason == None:
                    reason = str("Не вказана")
                await member.ban(reason = reason)
                bembed = discord.Embed(color = 0x22ff00, description = f"Учасник {member.name} був забанений користувачем {author.mention} по причині \"{reason}\".")
                await ctx.send(embed = bembed, delete_after = 30)
    if role.permissions.ban_members == False:
        notperms = discord.Embed(color = 0xff0000, description = "Вибач, але в тебе не має права використовувати цю команду!")
        await ctx.send(embed = notperms, delete_after = 30)



#соц. команди

#Обійми
@bot.command()
async def hug(ctx, member: discord.Member = None):
    await ctx.channel.purge(limit = 1)
    this_server_id = None
    for x in server_id:
        if ctx.guild.id == x:
            this_server_id = 1
    if member == None:
        await ctx.send("Вибач, але команда була введене не вірно, ти забув ввести того кого хотів обійняти.\nСпробуй так: `+hug @sorao`")
    author = ctx.author

    embed = discord.Embed(
        color = 0x22ff00,
        description = f"{author.mention} обійняв {member.mention}")
    embed.set_image(url=f'{random.choice(img_link.img_hug)}')
    #футер
    author = ctx.message.author
    if footer_swich == 1 and this_server_id == 1:
        embed.set_footer(text=f"Команду запросив {author}", icon_url=author.avatar_url)
    await ctx.send(embed=embed)

#привітати
@bot.command()
async def hi(ctx, member: discord.Member = None):
    await ctx.channel.purge(limit = 1)
    this_server_id = None
    for x in server_id:
        if ctx.guild.id == x:
            this_server_id = 1
    if member == None:
        await ctx.send("Вибач, але команда була введене не вірно, ти забув ввести того кого хотів  привітати.\nСпробуй так: `+hi @sorao`")
    author = ctx.author

    embed = discord.Embed(
        color = 0x22ff00,
        description = f"{author.mention} привітав {member.mention}")
    embed.set_image(url=f'{random.choice(img_link.img_hi)}')
    #футер
    author = ctx.message.author
    if footer_swich == 1 and this_server_id == 1:
        embed.set_footer(text=f"Команду запросив {author}", icon_url=author.avatar_url)
    await ctx.send(embed=embed)

#Прощання
@bot.command()
async def bye(ctx, member: discord.Member = None):
    await ctx.channel.purge(limit = 1)
    this_server_id = None
    for x in server_id:
        if ctx.guild.id == x:
            this_server_id = 1
    if member == None:
        await ctx.send("Вибач, але команда була введене не вірно, ти забув ввести того кого хотів обійняти.\nСпробуй так: `+hug @sorao`")
    author = ctx.author

    embed = discord.Embed(
        color = 0x22ff00,
        description = f"{author.mention} обійняв {member.mention}")
    embed.set_image(url=f'{random.choice(img_link.img_hug)}')
    #футер
    author = ctx.message.author
    if footer_swich == 1 and this_server_id == 1:
        embed.set_footer(text=f"Команду запросив {author}", icon_url=author.avatar_url)
    await ctx.send(embed=embed)


#відобразити аватар учасника
@bot.command()
async def avatar(ctx, member: discord.Member  = None):
    await ctx.channel.purge(limit = 1)
    this_server_id = None
    for x in server_id:
        if ctx.guild.id == x:
            this_server_id = 1
    if member == None:
        member = ctx.author
    embed = discord.Embed(color = 0x22ff00, title = f"Аватар учасника - {member.name}", description = f"[Нажми, щоб завантажити аватар]({member.avatar_url})")
    embed.set_image(url = member.avatar_url)
    if footer_swich == 1 and this_server_id == 1:
        embed.set_footer(text=f"Команду запросив {ctx.author}", icon_url=ctx.author.avatar_url)
    await ctx.send(embed = embed)


#відображає інформацію про аніме(не доброблена, добавити футер)
@bot.command()
async def anime(ctx, *,querry):
    await ctx.channel.purge(limit = 1)
    this_server_id = None
    for x in server_id:
        if ctx.guild.id == x:
            this_server_id = 1
    try:
        anime = animec.Anime(querry) # searching animes 
    except:
        await ctx.send(embed = discord.Embed(color = 0xff0000, description = f"Аніме зацією назвою не знайдено! Спробуй ввести назву на латиниці\nДетальніше: `+help anime` "))     # if the provided anime name is invalid
        return
    embed = discord.Embed(title = anime.name, description = f"{anime.description[100:]}...", color=0x22ff00)
    embed.add_field(name = "Альтернативні назви:", value = f"{str(anime.title_jp)}, {str(anime.alt_titles)}", inline = True)
    embed.add_field(name = "Продюсери: ", value = str(', '.join(anime.producers)), inline = True)
    embed.add_field(name = "Серій:", value = str(anime.episodes), inline = True)
    embed.add_field(name = "Жанр:" , value = str(', '.join(anime.genres)), inline = True)
    embed.add_field(name = "Рейтинг:", value = str(anime.rating), inline = True)
    broadcast = str(anime.broadcast)
    if broadcast == None:
        broadcast = str("Не вказано")
    embed.add_field(name = "Перший показ:", value = broadcast, inline = True)
    status = str(anime.status)
    if status == "Finished Airing":
        status = "Завершений"
    elif status == "Currently Airing":
        status = "Зараз виходить"
    embed.add_field(name = "Статус: ", value = str(status), inline = True)
    type = str(anime.type)
    if type == "Movie":
        type = "Фільм"
    elif type == "TV":
        type = "ТВ серіал"
    embed.add_field(name = "Тип:", value = str(type), inline = True)
    embed.set_image(url = anime.poster)
    
    await ctx.send(embed = embed)


@bot.command()
async def help(ctx):
    await ctx.channel.purge(limit = 1)
#Перша сторінка еибеда
    this_server_id = None
    for x in server_id:
        if ctx.guild.id == x:
            this_server_id = 1
    em1 = discord.Embed(color = 0x22ff00, title = "Список команд")
    em1.add_field(name = "+fswitch `<вкл/вимк>`", value = "Дозволяє увімкнути або вимкнути у повідомленнях футер. Приймається два аргументи: `вкл` й `вимк`\nДетальніше: `+help fswitch`", inline = False)
    em1.add_field(name = "+clear `<10>`", value = "Видаляє останні повідомлення в чаті.\nДетальніше: `+help clear`", inline = False)
    em1.add_field(name = "+userinfo `@Sorao#0329`", value = "Вивожить інформаці про певного користувача.\nДетальніше: `+help userinfo`", inline = False)
    em1.add_field(name = "+sinfo", value = "Виводить інформацію про сервер.", inline = False)
    em1.add_field(name = "+memmes", value = "Виводить випадковий мем із Reddit", inline = False)
    em1.set_thumbnail(url = "https://cdn.discordapp.com/avatars/927164571138015232/f6a03b28930d394f3025cc2291ab1b6e.webp")
    if footer_swich == 1 and this_server_id == 1:
        em1.set_footer(text=f"Команду запросив {ctx.author}", icon_url=ctx.author.avatar_url)

#Друга сторінка ембеда
    this_server_id = None
    for x in server_id:
        if ctx.guild.id == x:
            this_server_id = 1
    em2 = discord.Embed(color = 0x22ff00, title = "Список команд")
    em2.add_field(name = "+kick `<@Sorao#0329>`", value = "Дозволяє вигнати користувача із сервера.\nДетальніше: `+help kick`", inline = False)
    em2.add_field(name = "+ban `<@Sorao#0329>`", value = "Забоковує учасникові доступ до сервера.\nДетальніше: `+help ban`", inline = False)
    em2.add_field(name = "+hug `<@Sorao#0329>`", value = "Обійняти учасника сервера\nДетальніше: `+help hug`", inline = False)
    em2.add_field(name = "+whatanime <назва>", value = "Шукає інформацію про аніме.\nДетальніше: `+help wahatanime`", inline = False)
    em2.add_field(name = "+avatar `<@Sorao#0329>`", value = "Виводить аватар учасника\nДетальніше: `+help avatar`", inline = False)
    em2.set_thumbnail(url = "https://cdn.discordapp.com/avatars/927164571138015232/f6a03b28930d394f3025cc2291ab1b6e.webp")
    if footer_swich == 1 and this_server_id == 1:
        em2.set_footer(text=f"Команду запросив {ctx.author}", icon_url=ctx.author.avatar_url)   

#Третя сторінка
    this_server_id = None
    for x in server_id:
        if ctx.guild.id == x:
            this_server_id = 1
    em3 = discord.Embed(color = 0x22ff00, title = "Список команд")
    em3.add_field(name = "+play `<music name>`", value = "Відтворює пісню в голосовому чаті за назвою чи URL\nДетальніше: `+help play`", inline = False)
    em3.add_field(name = "+join", value = "Запросити бота до госового канул(де ви зараз знаходитесь).\nДетальніше: `+help join`", inline = False)
    em3.add_field(name = "+summon `<ID канала>`", value = "Запрошує бота в голосовий канал, в певний канала(по ID каналу).\nДетальніше: `+help summon`", inline = False)
    em3.add_field(name = "+leave", value = "Виганяє бота з голосового каналу.\nДетальніше: `+help leave`", inline = False)
    em3.add_field(name = "+now", value = "Показуэ, що зараз грає.\nДетальніше: `+help now`", inline = False)
    em3.set_thumbnail(url = "https://cdn.discordapp.com/avatars/927164571138015232/f6a03b28930d394f3025cc2291ab1b6e.webp")
    if footer_swich == 1 and this_server_id == 1:
        em3.set_footer(text=f"Команду запросив {ctx.author}", icon_url=ctx.author.avatar_url) 
    
#групуємо всі ембеди в один список, щоб викликати пізніше
    contents = [em1, em2, em3]
#oages: скільки сторінок ви хочете
#сur_page: Повідомляє вам, яка поточна сторінка. **1 = коли команда викликається, вона починається з 1**
#message: Надсилає наведене вище ембди, **Переконайтеся, що embed=contents** 
    pages = 3
    cur_page = 1
    message = await ctx.send(embed=contents[cur_page - 1])


#Вказує боту додати такі смайли реакції до щойно надісланого повідомлення
    await message.add_reaction("◀️")
    await message.add_reaction("▶️")

#Перевіряє, щоб лише той, хто викликає команду, міг взаємодіяти з ембедом**Щоб не було хаооу**
    def check(reaction, user):
        return user == ctx.author and str(reaction.emoji) in ["◀️", "▶️"]


    while True:
        try:
#**timeout=None** Без обмежень у часі, якщо немає реакції
#Наприклад, якщо **timeout = 60** тоді повідомлення видаляється після 60-ти секунд 
            reaction, user = await bot.wait_for("reaction_add", timeout = 60, check = check)

            if str(reaction.emoji) == "▶️" and cur_page != pages:
                cur_page += 1
                await message.edit(
                    embed=contents[cur_page - 1]
                    )
                await message.remove_reaction(reaction, user)

            elif str(reaction.emoji) == "◀️" and cur_page > 1:
                cur_page -= 1
                await message.edit(
                    embed=contents[cur_page - 1]
                    )
                await message.remove_reaction(reaction, user)

            else:
                await message.remove_reaction(reaction, user)

        except asyncio.TimeoutError:
            await message.delete()
            break


bot.run("NOKEN_BOT")
