"""Personal garden card and durable, deduplicated direct notifications."""
import asyncio
from datetime import UTC, datetime
import discord
from discord import app_commands
from discord.ext import tasks
from lafamily.async_database import database_call
from lafamily.garden_store import CROPS, hour_at
from lafamily.errors import UserError
from lafamily.responses import send_error
from lafamily.diagnostics import log_error


class GardenService:
    def __init__(self, client, store, guild_id, clock=None):
        self.client, self.store, self.guild_id = client, store, guild_id
        self.clock = clock or (lambda: datetime.now(UTC))
        self.lock = asyncio.Lock()

    def restoring(self):
        return bool(getattr(getattr(self.client, 'backups', None), 'restoring', False))

    def check(self, interaction, owner):
        if interaction.guild_id != self.guild_id or interaction.user.id != owner:
            raise UserError('Этот сад доступен только его владельцу на сервере семьи.')
        if self.restoring():
            raise UserError('База восстанавливается. Откройте /сад после переподключения.')

    async def card(self, owner, selected=None):
        now = datetime.now(UTC)
        rows = await database_call(self.store,'garden_snapshot',str(self.guild_id),str(owner),now)
        embed = discord.Embed(title='🌱 Ваш сад', colour=0x4CAF50)
        lines=[]
        for r in rows:
            name, required=CROPS[r['crop']]
            if r['ripe_hour'] is not None:
                state='✅ Созрело — можно собрать'
            else:
                state=f"PayDay: {r['progress']}/{required} · осталось {required-r['progress']}"
                if r['planted_hour']==hour_at(now):
                    state+=' · обработка со следующего часа'
                else:
                    state+=' · '+('обработано в этом часу' if r['processed_hour']==hour_at(now) else 'нужна обработка')
            lines.append(f"**#{r['id']} {name} ×{r['quantity']}**\n{state}\nУдобрение: {'да' if r['fertilized'] else 'нет'} · урожай: {r['quantity']*(16 if r['fertilized'] else 12)} шт.")
        embed.description='\n\n'.join(lines) or 'Сад пуст. Нажмите «Посадить» и укажите количества.'
        embed.set_footer(text=f"Занято {sum(r['quantity'] for r in rows)}/9 · Разрешите ЛС от бота для напоминаний и созревания.")
        embed.add_field(name='Правила',value='Первый PayDay засчитывается автоматически. Далее обрабатывайте каждый час для зачёта следующего PayDay. На :50 напомню в ЛС. Удобрение один раз: 16 вместо 12 шт. с растения. Созревшие занимают места до сбора.',inline=False)
        return embed, GardenView(self,owner,rows,selected)

    async def tick(self, now=None):
        if self.restoring(): return
        clock = (lambda: now) if now is not None else self.clock
        now=clock()
        async with self.lock:
            for user in await database_call(self.store,'garden_owners',str(self.guild_id)):
                if self.restoring(): return
                events=await database_call(self.store,'garden_pending',str(self.guild_id),user,clock())
                for kind,key in events:
                    if self.restoring(): return
                    # Fetch before claiming; a failure before send can be retried safely.
                    try:
                        recipient=await self.client.fetch_user(int(user))
                    except discord.HTTPException as error:
                        log_error('Не удалось найти получателя сада',error)
                        continue
                    if self.restoring(): return
                    claimed=await database_call(self.store,'garden_claim',str(self.guild_id),user,kind,key,clock())
                    if not claimed: continue
                    if self.restoring(): return
                    names=', '.join(f"{CROPS[r['crop']][0]} ×{r['quantity']}" for r in claimed)
                    text=(f'🌱 Пора обработать сад в этом часу: {names}. Откройте /сад и отметьте обработку.' if kind=='process' else
                          f"🌾 Созрело: {names}. Урожай: {sum(r['quantity']*(16 if r['fertilized'] else 12) for r in claimed)} шт. Откройте /сад, чтобы собрать.")
                    state='sent'
                    try:
                        await recipient.send(text,allowed_mentions=discord.AllowedMentions.none())
                    except discord.Forbidden as error:
                        state='forbidden'; log_error('ЛС сада закрыты',error)
                    except discord.HTTPException as error:
                        state='uncertain'; log_error('Не удалось отправить ЛС сада',error)
                    except (OSError, asyncio.TimeoutError) as error:
                        state='uncertain'; log_error('Неизвестен результат ЛС сада',error)
                    await database_call(self.store,'garden_delivery_result',str(self.guild_id),user,kind,key,state)

    def start(self):
        if not self.poll.is_running(): self.poll.start()

    async def close(self):
        task=self.poll.get_task(); self.poll.cancel()
        if task:
            try: await task
            except asyncio.CancelledError: pass

    @tasks.loop(seconds=30)
    async def poll(self):
        try:
            if self.client.is_ready(): await self.tick()
        except Exception as error:
            log_error('Не удалось проверить сад',error)


class PlantModal(discord.ui.Modal, title='Новая посадка'):
    def __init__(self, service, owner):
        super().__init__()
        self.service,self.owner=service,owner
        self.inputs={}
        for crop,(name,_) in CROPS.items():
            field=discord.ui.TextInput(label=name,default='0',max_length=2)
            self.inputs[crop]=field; self.add_item(field)

    async def on_submit(self, interaction):
        self.service.check(interaction,self.owner)
        values={}
        for crop,field in self.inputs.items():
            if not field.value.isascii() or not field.value.isdecimal():
                raise UserError('Введите целые количества от 0 до 9.')
            values[crop]=int(field.value)
        await interaction.response.defer(ephemeral=True)
        await database_call(self.service.store,'garden_plant',str(self.service.guild_id),str(self.owner),str(interaction.id),values)
        embed,view=await self.service.card(self.owner)
        await interaction.edit_original_response(content='Посадка сохранена.',embed=embed,view=view)

    async def on_error(self, interaction, error):
        await send_error(interaction,error)


class GardenView(discord.ui.View):
    def __init__(self,service,owner,rows,selected=None):
        super().__init__(timeout=900)
        self.service,self.owner=service,owner
        self.selected=selected if any(r['id']==selected for r in rows) else None
        if rows:
            options=[discord.SelectOption(label='Весь сад',value='all',default=self.selected is None)]
            options += [discord.SelectOption(label=f"#{r['id']} {CROPS[r['crop']][0]} ×{r['quantity']}",value=str(r['id']),default=r['id']==self.selected) for r in rows]
            select=discord.ui.Select(placeholder='Выберите посадку или весь сад',options=options,row=0)
            async def choose(interaction):
                self.selected=None if select.values[0]=='all' else int(select.values[0])
                await self.refresh(interaction)
            select.callback=choose; self.add_item(select)

    async def interaction_check(self, interaction):
        self.service.check(interaction,self.owner)
        return True

    async def on_error(self,interaction,error,item):
        await send_error(interaction,error)

    async def refresh(self,interaction,content=None):
        if not interaction.response.is_done(): await interaction.response.defer()
        embed,view=await self.service.card(self.owner,self.selected)
        await interaction.edit_original_response(content=content,embed=embed,view=view)

    @discord.ui.button(label='Посадить',style=discord.ButtonStyle.success,row=1)
    async def plant(self,interaction,button):
        await interaction.response.send_modal(PlantModal(self.service,self.owner))

    async def action(self,interaction,action):
        await interaction.response.defer()
        rows=await database_call(self.service.store,'garden_action',str(self.service.guild_id),str(self.owner),action,self.selected)
        content='Отметка сохранена.'
        if action=='collect':
            content=f"Собрано: {sum(r['quantity']*(16 if r['fertilized'] else 12) for r in rows)} шт. Места освобождены."
        await self.refresh(interaction,content)

    @discord.ui.button(label='Обработал в этом часу',row=1)
    async def process(self,interaction,button): await self.action(interaction,'process')

    @discord.ui.button(label='Удобрил',row=1)
    async def fertilize(self,interaction,button): await self.action(interaction,'fertilize')

    @discord.ui.button(label='Собрать',row=2)
    async def collect(self,interaction,button): await self.action(interaction,'collect')

    @discord.ui.button(label='Обновить',row=2)
    async def update(self,interaction,button): await self.refresh(interaction)


def register_garden(tree,service,guild_id):
    @tree.command(name='сад',description='Посадки, обработка, удобрение и сбор личного сада',guild=discord.Object(id=guild_id),extras={'access':'all'})
    @app_commands.guild_only()
    async def garden(interaction: discord.Interaction):
        service.check(interaction,interaction.user.id)
        await interaction.response.defer(ephemeral=True)
        embed,view=await service.card(interaction.user.id)
        await interaction.edit_original_response(embed=embed,view=view)
