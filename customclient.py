"""
CustomClient is a specialized subclass of `discord.ext.commands.Bot` to facilitate the S.A.M. bot functionality.

*Features*:
    - Integrates with SQLAlchemy for database operations.
    - Uses an asynchronous task queue for handling various player and unit-related tasks.
    - Periodically keeps the database session alive.
    - Manages bot commands and extensions.
    - Syncs slash commands with Discord's command tree.
    - Employs error handling for database session management.

Modules:
    - `discord.ext.commands`: Provides command-related tools.
    - `discord.app_commands`: Manages Discord's application commands.
    - `sqlalchemy.orm.Session`: Database session handling.
    - `logging`: Logging utilities for debugging and information.
"""

from discord import Interaction, Intents, Status, Activity, ActivityType, Member
from discord.ext.commands import Bot
from discord.ext import tasks
from os import getenv
from sqlalchemy.orm import Session
from models import *
from sqlalchemy import text
from datetime import datetime
from typing import Any, Callable
from singleton import Singleton
import asyncio
import templates
import logging
from utils import uses_db, RollingCounterDict

use_ephemeral = getenv("EPHEMERAL", "false").lower() == "true"

logging.basicConfig(level=logging.getLevelName(getenv("LOG_LEVEL", "INFO")))
logger = logging.getLogger(__name__)
logging.getLogger("discord").setLevel(logging.WARNING)

@Singleton
class CustomClient(Bot): # need to inherit from Bot to use Cogs
    """
    CustomClient is a subclass of discord.Bot that adds additional functionality for the S.A.M. bot.

    Attributes:
        - `mod_roles`: (Set[str]) Roles with moderator privileges.
        - `session`: (Session) Database session for executing SQL queries.
        - `use_ephemeral`: (bool) Controls whether to send messages as ephemeral.
        - `config`: (dict) Bot configuration loaded from the database.
        - `uses_db`: (Callable) A decorator for database operations.
    """
    mod_roles = {1308924912936685609, 1302095620231794698}
    gm_role = 1308925031069388870
    session: Session
    use_ephemeral: bool
    config: dict
    sessionmaker: Callable
    start_time: datetime
    def __init__(self, session: Session,/, sessionmaker: Callable, **kwargs):
        """
        Initializes the CustomClient instance.

        Args:
            session (Session): The SQLAlchemy session for database operations.
            **kwargs: Additional keyword arguments for the Bot constructor.

        Merges the `DEFAULTS` with provided `kwargs`, loads configurations, and initializes
        the task queue. Additionally, loads or initializes `BOT_CONFIG` and `MEDAL_EMOTES` in the database.
        """
        defintents = Intents.default()
        defintents.members = True
        DEFAULTS = {"command_prefix":"\0", "intents":defintents}
        kwargs = {**DEFAULTS, **kwargs} # merge DEFAULTS and kwargs, kwargs takes precedence
        super().__init__(**kwargs)
        self.owner_ids = {533009808501112881, 126747253342863360}
        self.sessionmaker = sessionmaker
        self.queue = asyncio.Queue()
        _Config = session.query(Config).filter(Config.key == "BOT_CONFIG").first()
        if not _Config:
            _Config = Config(key="BOT_CONFIG", value={"EXTENSIONS":[]})
            session.add(_Config)
            session.commit()
        self.config:dict = _Config.value
        _Medal_Emotes = session.query(Config).filter(Config.key == "MEDAL_EMOTES").first()
        if not _Medal_Emotes:
            _Medal_Emotes = Config(key="MEDAL_EMOTES", value={})
            session.add(_Medal_Emotes)
            session.commit()
        self.medal_emotes:dict = _Medal_Emotes.value
        self.use_ephemeral = use_ephemeral
        self.tree.interaction_check = self.check_banned_interaction

    async def check_banned_interaction(self, interaction: Interaction):
        # check if the user.id is in the BANNED_USERS env variable, if so, reply with a message and return False, else return True
        logger.debug(f"Interaction check for user {interaction.user.global_name}")
        banned_users = getenv("BANNED_USERS", "").split(",")
        if not banned_users[0]: # if the env was empty, split returns [""], so we need to check for that
            logger.debug(f"Interaction check passed for user {interaction.user.global_name}")
            return True
        banned_users = [int(user) for user in banned_users]
        if interaction.user.id in banned_users:
            await interaction.response.send_message("You are banned from using this bot", ephemeral=self.use_ephemeral)
            logger.warning(f"Interaction check failed for user {interaction.user.global_name}")
            return False
        logger.debug(f"Interaction check passed for user {interaction.user.global_name}")
        return True

    async def resync_config(self, session: Session):
        """
        Synchronizes the bot configuration with the database.

        Fetches and updates the in-memory configuration, ensuring the bot is synchronized with the database.

        Raises:
            SQLAlchemyError: If a database operation fails.
        """
        _Config = session.query(Config).filter(Config.key == "BOT_CONFIG").first()
        _Config.value = self.config
        logger.debug(f"Resynced config: {self.config}")

    async def queue_consumer(self, session: Session):
        """
        Consumes tasks from the queue for processing player and unit actions.

        *Task Types*:
            - **0**: Creation tasks.
            - **1**: Update tasks.
            - **2**: Deletion tasks.
            - **4**: Graceful termination of the queue consumer.

        This method runs indefinitely, processing tasks from the queue and managing associated database
        operations, while updating channels as necessary.

        Raises:
            Exception: If task processing encounters an error.
        """
        logger.info("queue consumer started")
        unknown_handler = lambda task: logger.error(f"Unknown task type: {task}")
        handlers = {
            0: self._handle_create_task,
            1: self._handle_update_task,
            2: self._handle_delete_task,
            4: self._handle_terminate_task
        }
        ratelimit = RollingCounterDict(30)
        nosleep = True
        
        while True:
            if nosleep:
                await asyncio.sleep(0)
                nosleep = False
            else:
                await asyncio.sleep(5)  # Maintain pacing to avoid hitting downstream timeouts
            logger.debug(f"Queue size: {self.queue.qsize()}")
            if self.queue.qsize() >= 400:
                logger.critical(f"Queue size is {self.queue.qsize()}, this is too high!")
                # fetch the discord user for the bot owner, message them, then call self.close()
                owner = await self.fetch_user(533009808501112881)
                if owner:
                    await owner.send("Queue size is too high, terminating")
                await self.close()
            task = await self.queue.get()
            if not isinstance(task, tuple):
                logger.error("Task is not a tuple, skipping")
                nosleep = True
                continue
            # we need to recraft the tuple before we can log it, because the session is detached
            if len(task) == 3:
                task = (task[0], session.merge(task[1]), task[2])
            elif len(task) == 2:
                task = (task[0], session.merge(task[1]), 0)
            elif len(task) == 1:
                if not task[0] == 4:
                    logger.error("Task is a tuple of length 1, but the first element is not 4, skipping")
                    nosleep = True
                    continue
            else:
                logger.error(f"Task is a tuple of length {len(task)}, skipping")
                nosleep = True
                continue
            ratelimit.set(str(task[1]))
            if ratelimit.get(str(task[1])) >=5: # window is 30s, which is 6 tasks, this requires at least 50% different tasks
                logger.warning(f"Ratelimit hit for {task[1]}")
                nosleep = True
                continue # just discard the task
            #logger.debug(f"Processing task: {task}")
            # Initialize fail count
            fail_count = task[2] if len(task) > 2 else 0
            if fail_count > 5:
                logger.error(f"Task {task} failed too many times, skipping")
                nosleep = True
                continue

            try:
                result = await handlers.get(task[0], unknown_handler)(task)
                #session.expunge(task[1]) # expunge the instance to avoid memory leak or stale commits
                if result:
                    break
            except Exception as e:
                logger.error(f"Error processing task: {e}")
                # Requeue the task with an incremented fail count
                new_fail_count = fail_count + 1
                if len(task) > 2:
                    task = (*task[:2], new_fail_count)  # Update fail count
                else:
                    task = (*task, new_fail_count)  # Add fail count
                self.queue.put_nowait(task)
            self.queue.task_done()

    # we are going to start subdividing the queue consumer into multiple functions, for clarity

    async def _handle_create_task(self, task: tuple[int, Any], session: Session):
        session.execute(text("SET SESSION innodb_lock_wait_timeout = 10"))
        requeued = False
        if task[1].id is None:
            logger.error(f"Task has a None id, skipping")
            return
        instance  = session.query(task[1].__class__).filter(task[1].__class__.id == task[1].id).first()
        if isinstance(instance, Player):
            player = instance
            if self.config.get("dossier_channel_id"):
                medals = session.query(Medals).filter(Medals.player_id == player.id).all()
                # identify what medals have known emotes
                known_emotes = set(self.medal_emotes.keys())
                known_medals = {medal.name for medal in medals if medal.name in known_emotes}
                unknown_medals = {medal.name for medal in medals if medal.name not in known_emotes}
                known_medals_list = list(known_medals)
                # make rows of 5 medals that have known emotes
                rows = [known_medals_list[i:i+5] for i in range(0, len(known_medals_list), 5)]
                unknown_medals_list = list(unknown_medals)
                unknown_text = "\n".join(unknown_medals_list)
                # convert the rows to a string of emotes, with a space between each emote
                medal_block = "\n".join([" ".join([self.medal_emotes[medal] for medal in row]) for row in rows]) + "\n" + unknown_text
                mention = await self.fetch_user(player.discord_id)
                mention = mention.mention if mention else ""
                # check for an existing dossier message, if it exists, skip creation
                create_dossier = True
                existing_dossier = session.query(Dossier).filter(Dossier.player_id == player.id).first()
                if existing_dossier:
                    # check if the message itself actually exists
                    channel = self.get_channel(self.config["dossier_channel_id"])
                    if channel:
                        message = await channel.fetch_message(existing_dossier.message_id)
                        if message:
                            logger.debug(f"Dossier message for player {player.id} already exists, skipping creation")
                            create_dossier = False
                if not player.id:
                    logger.error(f"missing player id, skipping dossier creation")
                    return
                if create_dossier:
                    dossier_message = await self.get_channel(self.config["dossier_channel_id"]).send(templates.Dossier.format(mention=mention, player=player, medals=medal_block))
                    dossier = Dossier(player_id=player.id, message_id=dossier_message.id)
                    session.add(dossier)
                    logger.debug(f"Created dossier for player {player.id} with message ID {dossier_message.id}")
            if self.config.get("statistics_channel_id"):
                unit_message = await self.generate_unit_message(player)
                _player = session.merge(player)
                discord_id = _player.discord_id
                mention = await self.fetch_user(discord_id)
                mention = mention.mention if mention else ""
                # check for an existing statistics message, if it exists, skip creation
                existing_statistics = session.query(Statistic).filter(Statistic.player_id == _player.id).first()
                if existing_statistics:
                    # check if the message itself actually exists
                    channel = self.get_channel(self.config["statistics_channel_id"])
                    if channel:
                        message = await channel.fetch_message(existing_statistics.message_id)
                        if message:
                            logger.debug(f"Statistics message for player {_player.id} already exists, skipping creation")
                            return
                if not _player.id:
                    logger.error(f"missing player id, skipping statistics creation")
                    return
                statistics_message = await self.get_channel(self.config["statistics_channel_id"]).send(templates.Statistics_Player.format(mention=mention, player=_player, units=unit_message))
                statistics = Statistic(player_id=_player.id, message_id=statistics_message.id)
                session.add(statistics)
                logger.debug(f"Created statistics for player {_player.id} with message ID {statistics_message.id}")
        elif isinstance(instance, Unit):
            player = session.query(Player).filter(Player.id == instance.player_id).first()
            if player:
                if not requeued:
                    self.queue.put_nowait((1, player))
                    logger.debug(f"Queued update task for player {player.id} due to unit {instance.id} Location 1")
                    requeued = True
                else:
                    logger.debug(f"Already queued update task for player {player.id} due to unit {instance.id} Location 1")
            else:
                logger.error(f"Player not found for unit {instance.id}")
        elif isinstance(instance, PlayerUpgrade):
            unit = session.query(Unit).filter(Unit.id == instance.unit_id).first()
            player = session.query(Player).filter(Player.id == unit.player_id).first()
            if player:
                if not requeued:
                    self.queue.put_nowait((1, player))
                    logger.debug(f"Queued update task for player {player.id} due to upgrade {instance.id} Location 2")
                    requeued = True
                else:
                    logger.debug(f"Already queued update task for player {player.id} due to upgrade {instance.id} Location 2")

    async def _handle_update_task(self, task: tuple[int, Any], session: Session):
        session.execute(text("SET SESSION innodb_lock_wait_timeout = 10"))
        instance = session.query(task[1].__class__).filter(task[1].__class__.id == task[1].id).first()
        requeued = False
        if isinstance(instance, Player):
            logger.debug(f"Updating player: {instance}")
            player = instance
            logger.debug("fetching dossier")
            dossier = session.query(Dossier).filter(Dossier.player_id == player.id).first()
            if dossier:
                logger.debug("dossier found, fetching channel")
                channel = self.get_channel(self.config["dossier_channel_id"])
                if channel:
                    logger.debug("channel found, fetching message")
                    message = await channel.fetch_message(dossier.message_id)
                    logger.debug("message found, fetching user")
                    mention = await self.fetch_user(player.discord_id)
                    mention = mention.mention if mention else ""
                    logger.debug("user found, editing message")
                    await message.edit(content=templates.Dossier.format(mention=mention, player=player, medals=""))
                    logger.debug(f"Updated dossier for player {player.id} with message ID {dossier.message_id}")
            else:
                logger.debug("no dossier found, pushing create task")
                if not requeued:
                    self.queue.put_nowait((0, player))
                    logger.debug(f"Queued create task for player {player.id} due to missing dossier message Location 3")
                    requeued = True
                else:
                    logger.debug(f"Already queued create task for player {player.id} due to missing dossier message Location 3")
            statistics = session.query(Statistic).filter(Statistic.player_id == player.id).first()
            if statistics:
                channel = self.get_channel(self.config["statistics_channel_id"])
                if channel:
                    message = await channel.fetch_message(statistics.message_id)
                    discord_id = player.discord_id
                    unit_message = await self.generate_unit_message(player)
                    _player = session.merge(player)
                    _statistics = session.merge(statistics)
                    mention = await self.fetch_user(discord_id)
                    mention = mention.mention if mention else ""
                    await message.edit(content=templates.Statistics_Player.format(mention=mention, player=_player, units=unit_message))
                    logger.debug(f"Updated statistics for player {_player.id} with message ID {_statistics.message_id}")
                else:
                    # there should be a message, but the discord side was probably deleted by a mod
                    logger.error(f"No channel found for statistics message of player {player.id}, skipping")
            else:
                # user doesn't have a statistics message, push a create task on the user, to fudge it back
                if not requeued:
                    self.queue.put_nowait((0, player))
                    logger.debug(f"Queued create task for player {player.id} due to missing statistics message Location 4")
                    requeued = True
                else:
                    logger.debug(f"Already queued create task for player {player.id} due to missing statistics message Location 4")
        elif isinstance(instance, Unit):
            unit = instance
            player = session.query(Player).filter(Player.id == unit.player_id).first()
            if player:
                if not requeued:
                    self.queue.put_nowait((1, player))
                    logger.debug(f"Queued update task for player {player.id} due to unit {unit.id} Location 5")
                    requeued = True
                else:
                    logger.debug(f"Already queued update task for player {player.id} due to unit {unit.id} Location 5")
        elif isinstance(task[1], PlayerUpgrade):
            upgrade = task[1]
            unit = session.query(Unit).filter(Unit.id == upgrade.unit_id).first()
            player = session.query(Player).filter(Player.id == unit.player_id).first()
            if player:
                if not requeued:
                    self.queue.put_nowait((1, player))
                    logger.debug(f"Queued update task for player {player.id} due to upgrade {upgrade.id} Location 6")
                    requeued = True
                else:
                    logger.debug(f"Already queued update task for player {player.id} due to upgrade {upgrade.id} Location 6")

    async def _handle_delete_task(self, task: tuple[int, Any], session: Session):
        session.execute(text("SET SESSION innodb_lock_wait_timeout = 10"))
        logger.debug(f"requerying instance for delete task")
        with session.no_autoflush: # disable flush on delete, to avoid a reinsert
            instance = session.query(task[1].__class__).filter(task[1].__class__.id == task[1].id).first()
        logger.debug(f"instance found for delete task: {instance}") # we can't log the task as it's possibly unbound, but we can log the instance
        requeued = False
        if isinstance(instance, Dossier):
            dossier = instance
            channel = self.get_channel(self.config["dossier_channel_id"])
            if channel:
                message = await channel.fetch_message(dossier.message_id)
                await message.delete()
                logger.debug(f"Deleted dossier message ID {dossier.message_id} for player {dossier.player_id}")
        elif isinstance(instance, Statistic):
            statistic = instance
            channel = self.get_channel(self.config["statistics_channel_id"])
            if channel:
                message = await channel.fetch_message(statistic.message_id)
                await message.delete()
                logger.debug(f"Deleted statistics message ID {statistic.message_id} for player {statistic.player_id}")
        elif isinstance(instance, Unit):
            logger.debug(f"instance is a unit, expunging")
            session.expunge(instance)
            return
            unit = instance
            player = session.query(Player).filter(Player.id == unit.player_id).first()
            if player:
                if not requeued:
                    self.queue.put_nowait((1, player))
                    logger.debug(f"Queued update task for player {player.id} due to unit {unit.id} Location 7")
                    requeued = True
                else:
                    logger.debug(f"Already queued update task for player {player.id} due to unit {unit.id} Location 7")
        elif isinstance(instance, PlayerUpgrade):
            upgrade = instance
            unit = session.query(Unit).filter(Unit.id == upgrade.unit_id).first()
            player = session.query(Player).filter(Player.id == unit.player_id).first()
            if player:
                if not requeued:
                    self.queue.put_nowait((1, player))
                    logger.debug(f"Queued update task for player {player.id} due to upgrade {upgrade.id} Location 8")
                    requeued = True
                else:
                    logger.debug(f"Already queued update task for player {player.id} due to upgrade {upgrade.id} Location 8")
        if instance: # if the instance is not None, we need to expunge it, if the instance is None we can ignore it
            session.expunge(instance)

    async def _handle_terminate_task(self, task): 
        logger.debug("Queue consumer terminating")
        return True # this is the only function that returns a value, as that's how we'll know to terminate, is if a value or raise is returned

    stats_map = {
        "INFANTRY": templates.Infantry_Stats,
        "MEDIC": templates.Non_Combat_Stats,
        "ENGINEER": templates.Non_Combat_Stats,
        "ARTILLERY": templates.Artillery_Stats,
        "MAIN_TANK": templates.Armor_Stats,
        "LIGHT_VEHICLE": templates.Armor_Stats,
        "LOGISTIC": templates.Armor_Stats,
        "BOMBER": templates.Air_Stats,
        "FIGHTER": templates.Air_Stats,
        "VTOL": templates.Air_Stats,
        "HVTOL": templates.Air_Stats,
        "HAT": templates.Air_Stats,
        "LIGHT_MECH": templates.Armor_Stats
    }

    async def generate_unit_message(self, player: Player, session: Session):
        """
        Creates a message detailing a player's units, both active and inactive.

        Args:
            player (Player): The player instance for whom the message is generated.

        Returns:
            str: Formatted unit messages for the player, grouped by status.
        """
        logger.debug(f"Generating unit message for player: {player.id}")
        unit_messages = []

        # Query inactive units
        units = session.query(Unit).filter(Unit.player_id == player.id).all()
        logger.debug(f"Found {len(units)} units for player: {player.id}")
        for unit in units:
            upgrades = session.query(PlayerUpgrade).filter(PlayerUpgrade.unit_id == unit.id).all()
            upgrade_list = ", ".join([upgrade.name for upgrade in upgrades])
            logger.debug(f"Unit {unit.name} of type {unit.unit_type} has status {unit.status.name}")
            logger.debug(f"Unit {unit.id} has upgrades: {upgrade_list}")
            unit_messages.append(templates.Statistics_Unit.format(unit=unit, upgrades=upgrade_list, callsign=('\"' + unit.callsign + '\"') if unit.callsign else ""))

        # Combine all unit messages into a single string
        combined_message = "\n".join(unit_messages)
        logger.debug(f"Generated unit message for player {player.id}: {combined_message}")
        return combined_message

    async def load_extensions(self, extensions: list[str]):
        """
        Loads a list of bot extensions (modules).

        Args:
            extensions (List[str]): List of extension names to load.
        """
        for extension in extensions:
            await self.load_extension(extension)
        logger.debug(f"Loaded extensions: {', '.join(extensions)}")

    async def set_bot_nick(self, nick: str):
        """
        Sets the bot's nickname across all connected guilds.

        Args:
            nick (str): The new nickname for the bot.
        """
        for guild in self.guilds:
            await guild.me.edit(nick=nick)

    async def on_ready(self):
        """
        Event handler for bot readiness.

        Called upon successful login, setting the bot's nickname, starting the queue consumer,
        and logging the bot's information.
        """
        logger.info(f"Logged in as {self.user}")
        #await self.set_bot_nick("S.A.M.")
        asyncio.create_task(self.queue_consumer())
        await self.change_presence(status=Status.online, activity=Activity(name="Meta Campaign", type=ActivityType.playing))
        if (getenv("STARTUP_ANIMATION", "false").lower() == "true"):
            try:
                self.startup_animation.start()
            except Exception as e:
                logger.error(f"Error starting startup animation: {e}")

    @tasks.loop(count=1)
    async def startup_animation(self):
        try:
            import sam_startup
        except ImportError:
            return
        channel = await self.fetch_channel(1211454073383952395)
        message =await channel.send(sam_startup.startup_sequence[0])
        for frame in sam_startup.startup_sequence[1:]:
            await message.edit(content=frame)
            await asyncio.sleep(1)
        self.startup_animation.cancel()
        self.notify_on_24_hours.start()

    @tasks.loop(count=1)
    async def notify_on_24_hours(self):
        logger.debug("Starting 24 hour notification loop")
        await asyncio.sleep(24 * 60 * 60)
        channel = await self.fetch_channel(1211454073383952395)
        owner = await self.fetch_user(533009808501112881)
        await channel.send(f"{owner.mention}\n# I have successfully survived 24 Hours!")
        logger.debug("24 hour notification loop finished")
        self.notify_on_24_hours.cancel()
    
    async def close(self, session: Session):
        """
        Closes the bot and performs necessary cleanup.

        Puts a termination signal in the queue, resyncs configuration, commits database changes,
        and closes the session.
        """
        await self.queue.put((4, None))
        await self.resync_config(session=session)
        await self.change_presence(status=Status.offline, activity=None)
        await super().close()

    async def setup_hook(self):
        """
        Sets up the bot's event listeners and syncs slash commands.

        This method is called when the bot is ready to receive events and commands. It:
            - Defines the `/ping` command
            - Loads core and additional extensions.
            - Synchronizes slash commands with Discord's command tree.
        """
        last_ping: datetime | None = None
        last_pinger: Member | None = None
        @self.tree.command(name="ping", description="Ping the bot")
        async def ping(interaction: Interaction):
            """
            Responds with "Pong!" and shows the bot's last restart time.

            *Displays:*
            - A message saying "Pong!" to indicate that the bot is active.
            - The last time the bot was restarted, both in a formatted and relative format.
            
            Example:
                `/ping` returns "Pong! I was last restarted at [formatted date], [relative time]."
            """
            nonlocal last_pinger, last_ping
            if interaction.user.id == last_pinger:
                await interaction.response.send_message("You've already pinged me recently, wait a bit before pinging again", ephemeral=True)
                return
            if last_ping and (datetime.now() - last_ping).total_seconds() < 10:
                await interaction.response.send_message("I've been pinged recently, wait a bit before pinging again", ephemeral=True)
                return
            last_pinger = interaction.user.id if not interaction.user.id == 533009808501112881 else last_pinger # don't lockout the owner from pings
            last_ping = datetime.now()
            await interaction.response.send_message(f"Pong! I was last restarted at <t:{int(self.start_time.timestamp())}:F>, <t:{int(self.start_time.timestamp())}:R>")

        await self.load_extension("extensions.debug") # the debug extension is loaded first and is always loaded
        #await self.load_extension("extensions.configuration") # for initial setup, we want to disable all user commands, so we only load the configuration extension
        await self.load_extensions(["extensions.admin", "extensions.configuration", "extensions.units", "extensions.shop", "extensions.companies", "extensions.backup", "extensions.search", "extensions.faq", "extensions.campaigns"]) # remaining extensions are currently loaded automatically, but will later support only autoloading extension that were active when it was last stopped

        logger.debug("Syncing slash commands")
        await self.tree.sync()
        logger.debug("Slash commands synced")

        # wrap all the consumer methods in uses_db now, since we can access the sessionmaker after init
        decorator = uses_db(sessionmaker=self.sessionmaker)
        self.queue_consumer = decorator(self.queue_consumer)
        self._handle_create_task = decorator(self._handle_create_task)
        self._handle_update_task = decorator(self._handle_update_task)
        self._handle_delete_task = decorator(self._handle_delete_task)
        self.generate_unit_message = decorator(self.generate_unit_message)
        self.close = decorator(self.close)
        
    async def start(self, *args, **kwargs):
        """
        Starts the bot with the provided arguments.

        Records the start time and retrieves the bot token from environment variables.

        Args:
            *args: Additional positional arguments.
            **kwargs: Additional keyword arguments.
        """
        self.start_time = datetime.now()
        logger.debug(f"Starting bot at {self.start_time}")
        await super().start(getenv("BOT_TOKEN"), *args, **kwargs)
        logger.debug(f"Bot has terminated at {datetime.now()}")