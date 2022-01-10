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
