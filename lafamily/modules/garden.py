"""Personal garden card and durable, deduplicated direct notifications."""
import asyncio
from datetime import UTC, datetime
import discord
from discord import app_commands
from discord.ext import tasks
from lafamily.async_database import database_call
from lafamily.garden_store import CROPS, GardenStore, hour_at
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
        now = self.clock()
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
            lines.append(f"**#{r['id']} {name} ×{r['quantity']}**\n{state}\nУдобрение: {'да' if r['fertilized'] else 'нет'}")
        embed.description='\n\n'.join(lines) or 'Сад пуст. Нажмите «Посадить» и укажите количества.'
        embed.set_footer(text=f"Занято {sum(r['quantity'] for r in rows)}/9 · Разрешите ЛС от бота для напоминаний и созревания.")
        embed.add_field(name='Сроки роста',value='\n'.join(f'{name} — {duration} PayDay' for name,duration in CROPS.values())+'\nPayDay — начало нового часа.',inline=False)
        embed.add_field(name='Правила',value='Первый PayDay засчитывается автоматически. Далее обрабатывайте каждый час для зачёта следующего PayDay. На :50 напомню об обработке и удобрении в ЛС. Удобрение отмечается один раз. Созревшие занимают места до сбора.',inline=False)
        embed.add_field(name='Добавление посадок', value='Выберите культуру и укажите количество. Поле «Осталось PayDay» необязательно: пустое означает обычный срок культуры. Если укажете остаток вручную, 0 означает созревшую посадку; для остальных отметьте обработку текущего часа для зачёта следующего PayDay.', inline=False)
        embed.add_field(name='Забыли отметку в боте?',value='Если в игре обработали растения, выберите одну посадку → «Исправить PayDay» и укажите остаток по игре. Обработку текущего часа отметьте отдельно.',inline=False)
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
                    claimed=await database_call(self.store,'garden_claim',str(self.guild_id),user,kind,key,clock)
                    if not claimed: continue
                    if self.restoring(): return
                    names=', '.join(f"{CROPS[r['crop']][0]} ×{r['quantity']}" for r in claimed)
                    if kind == 'process':
                        send_now=clock()
                        hour=hour_at(send_now)
                        if key != str(hour) or send_now.minute < 50:
                            await database_call(self.store,'garden_delivery_result',str(self.guild_id),user,kind,key,'expired')
                            continue
                        processing=', '.join(f"{CROPS[r['crop']][0]} ×{r['quantity']}" for r in claimed if GardenStore.garden_needs(r,hour)[0])
                        fertilizer=', '.join(f"{CROPS[r['crop']][0]} ×{r['quantity']}" for r in claimed if GardenStore.garden_needs(r,hour)[1])
                        parts=['🌱 Напоминание о саде.']
                        if processing: parts.append('Нужна обработка в этом часу: '+processing+'.')
                        if fertilizer: parts.append('Не отмечено удобрение: '+fertilizer+'.')
                        text='\n'.join(parts)+'\nОткройте /сад и отметьте выполненные действия.'
                    else:
                        text=f'🌾 Созрело: {names}. Откройте /сад, чтобы собрать.'
                    state='sent'
                    try:
                        options={'allowed_mentions':discord.AllowedMentions.none()}
                        if kind == 'process':
                            options['view']=reminder_view(str(self.guild_id),user,key,bool(processing),bool(fertilizer))
                        await recipient.send(text,**options)
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


class GardenReminderButton(discord.ui.DynamicItem[discord.ui.Button],
                           template=r'lafgarden:(?P<action>process|fertilize):(?P<guild>[0-9]+):(?P<owner>[0-9]+):(?P<hour>[0-9]+)'):
    def __init__(self, action, guild, owner, hour):
        self.action,self.guild,self.owner,self.hour=action,str(guild),str(owner),str(hour)
        super().__init__(discord.ui.Button(label='Обработано' if action=='process' else 'Удобрено',
            style=discord.ButtonStyle.success,custom_id=f'lafgarden:{action}:{guild}:{owner}:{hour}'))

    @classmethod
    async def from_custom_id(cls,interaction,item,match):
        return cls(match['action'],match['guild'],match['owner'],match['hour'])

    async def callback(self,interaction):
        try:
            service=interaction.client.garden
            author=getattr(getattr(interaction,'message',None),'author',None)
            if (interaction.guild_id is not None or str(service.guild_id)!=self.guild or
                str(interaction.user.id)!=self.owner or
                getattr(author,'id',None)!=getattr(interaction.client.user,'id',None)):
                raise UserError('Эта кнопка доступна только владельцу сада в личном напоминании бота.')
            if service.restoring():
                raise UserError('База восстанавливается. Повторите действие после переподключения.')
            await interaction.response.defer(ephemeral=True,thinking=True)
            count=await database_call(service.store,'garden_reminder_action',self.guild,self.owner,self.hour,self.action,service.clock)
            text=('Обработка отмечена.' if self.action=='process' else 'Удобрение отмечено.') if count else 'Уже отмечено или посадки больше не требуют этого действия.'
            await interaction.edit_original_response(content=text)
        except Exception as error:
            await send_error(interaction,error)


def reminder_view(guild,owner,hour,processing,fertilizer):
    view=discord.ui.View(timeout=None)
    if processing: view.add_item(GardenReminderButton('process',guild,owner,hour))
    if fertilizer: view.add_item(GardenReminderButton('fertilize',guild,owner,hour))
    return view


class CultureView(discord.ui.View):
    def __init__(self, service, owner):
        super().__init__(timeout=180)
        self.service,self.owner=service,owner
        select=discord.ui.Select(placeholder='Выберите культуру',options=[
            discord.SelectOption(label=name,value=crop) for crop,(name,_) in CROPS.items()])
        async def choose(interaction):
            service.check(interaction,owner)
            await interaction.response.send_modal(PlantModal(service,owner,select.values[0]))
        select.callback=choose
        self.add_item(select)

    async def interaction_check(self, interaction):
        self.service.check(interaction,self.owner)
        return True

    async def on_error(self, interaction, error, item):
        await send_error(interaction,error)


class PlantModal(discord.ui.Modal, title='Добавить посадку'):
    def __init__(self, service, owner, crop):
        super().__init__()
        if crop not in CROPS:
            raise UserError('Выберите культуру из списка.')
        self.service,self.owner,self.crop=service,owner,crop
        self.title=CROPS[crop][0]
        self.quantity=discord.ui.TextInput(label='Количество',placeholder='От 1 до 9',max_length=2)
        self.remaining=discord.ui.TextInput(label='Осталось PayDay',required=False,
            placeholder=f'По умолчанию: {CROPS[crop][1]}',max_length=2)
        self.add_item(self.quantity)
        self.add_item(self.remaining)

    async def on_submit(self, interaction):
        self.service.check(interaction,self.owner)
        quantity=self.quantity.value.strip()
        if not quantity.isascii() or not quantity.isdecimal() or not 1 <= int(quantity) <= 9:
            raise UserError('Количество растений: целое число от 1 до 9.')
        text=self.remaining.value.strip()
        remaining={}
        if text:
            if not text.isascii() or not text.isdecimal() or not 0 <= int(text) <= CROPS[self.crop][1]:
                raise UserError(f'Осталось PayDay: целое число от 0 до {CROPS[self.crop][1]}.')
            remaining[self.crop]=int(text)
        await interaction.response.defer(ephemeral=True)
        await database_call(self.service.store,'garden_plant',str(self.service.guild_id),str(self.owner),str(interaction.id),{self.crop:int(quantity)},remaining=remaining)
        embed,view=await self.service.card(self.owner)
        await interaction.edit_original_response(content='Посадка сохранена.',embed=embed,view=view)

    async def on_error(self, interaction, error):
        await send_error(interaction,error)


class CorrectPaydayModal(discord.ui.Modal, title='Исправить PayDay'):
    def __init__(self,service,owner,row):
        super().__init__()
        self.service,self.owner,self.row=service,owner,dict(row)
        self.remaining=discord.ui.TextInput(label='Осталось PayDay (по игре)',
            default=str(CROPS[row['crop']][1]-row['progress']),
            placeholder=f"От 0 до {CROPS[row['crop']][1]}",max_length=2)
        self.add_item(self.remaining)

    async def on_submit(self,interaction):
        self.service.check(interaction,self.owner)
        text=self.remaining.value.strip()
        if not text.isascii() or not text.isdecimal():
            raise UserError('Введите целое число оставшихся PayDay по игре.')
        await interaction.response.defer(ephemeral=True,thinking=True)
        await database_call(self.service.store,'garden_correct_remaining',str(self.service.guild_id),str(self.owner),
                            self.row['id'],int(text),self.row,str(interaction.id),self.service.clock)
        embed,view=await self.service.card(self.owner,self.row['id'])
        await interaction.edit_original_response(content='Остаток PayDay сохранён. Обработку текущего часа отметьте отдельно.',embed=embed,view=view)

    async def on_error(self,interaction,error):
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
        await interaction.response.send_message('Выберите культуру для посадки.',view=CultureView(self.service,self.owner),ephemeral=True)

    async def action(self,interaction,action):
        await interaction.response.defer()
        rows=await database_call(self.service.store,'garden_action',str(self.service.guild_id),str(self.owner),action,self.selected)
        content='Отметка сохранена.'
        if action=='collect':
            content='Растения собраны. Места освобождены.'
        await self.refresh(interaction,content)

    @discord.ui.button(label='Обработал в этом часу',row=1)
    async def process(self,interaction,button): await self.action(interaction,'process')

    @discord.ui.button(label='Удобрил',row=1)
    async def fertilize(self,interaction,button): await self.action(interaction,'fertilize')

    @discord.ui.button(label='Собрать',row=2)
    async def collect(self,interaction,button): await self.action(interaction,'collect')

    @discord.ui.button(label='Исправить PayDay',row=2)
    async def correct(self,interaction,button):
        if self.selected is None:
            raise UserError('Сначала выберите одну посадку в списке вместо «Весь сад».')
        rows=await database_call(self.service.store,'garden_snapshot',str(self.service.guild_id),str(self.owner),self.service.clock)
        row=next((r for r in rows if r['id']==self.selected),None)
        if row is None or row['ripe_hour'] is not None:
            raise UserError('Исправлять можно только растущую посадку. Обновите /сад.')
        await interaction.response.send_modal(CorrectPaydayModal(self.service,self.owner,row))

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
