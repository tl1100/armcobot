from logging import getLogger
from discord.ext.commands import GroupCog, Bot
from discord import Interaction, app_commands as ac, Member, TextStyle, Emoji
from discord.ui import Modal, TextInput
from models import Player, Unit, ActiveUnit, UnitStatus

logger = getLogger(__name__)

class Admin(GroupCog):
    def __init__(self, bot: Bot):
        self.bot = bot
        self.session = bot.session
        # self.interaction_check = self.is_mod # disabled for development, as those roles don't exist on the dev guild

    async def is_mod(self, interaction: Interaction):
        valid = any(interaction.user.has_role(role) for role in self.bot.mod_roles)
        if not valid:
            logger.warning(f"{interaction.user.name} tried to use admin commands")
        return valid
    
    @ac.command(name="recpoint", description="Give or remove a number of requisition points from a player")
    @ac.describe(player="The player to give or remove points from")
    @ac.describe(points="The number of points to give or remove")
    async def recpoint(self, interaction: Interaction, player: Member, points: int):
        # find the player by discord id
        player = self.session.query(Player).filter(Player.discord_id == player.id).first()
        if not player:
            await interaction.response.send_message("User doesn't have a Meta Campaign company", ephemeral=self.bot.use_ephemeral)
            return
        
        # update the player's rec points
        player.rec_points += points
        self.session.commit()
        logger.debug(f"User {player.name} now has {player.rec_points} requisition points")
        await interaction.response.send_message(f"{player.name} now has {player.rec_points} requisition points", ephemeral=self.bot.use_ephemeral)

    @ac.command(name="bulk_recpoint", description="Give or remove a number of requisition points from a set of players")
    @ac.describe(points="The number of points to give or remove")
    @ac.describe(status="Status of the unit (Inactive = 0, Active = 1, MIA = 2, KIA = 3)")
    async def bulk_recpoint(self, interaction: Interaction, status: str, points: int):
        # Find all units with corresponding Enum status
        status_enum = UnitStatus(status)
        units = self.session.query(Unit).filter(Unit.status == status_enum).all()
        for unit in units:
            # Find player of each unit and update their recpoints
            player = unit.player
            player.rec_points += points
            logger.debug(f"User {player.name} now has {player.rec_points} requisition points")
        self.session.commit()
        await interaction.response.send_message(f"Players of units of the status {status} have received {points} requisition points", ephemeral=self.bot.use_ephemeral)

    @ac.command(name="bonuspay", description="Give or remove a number of bonus pay from a player")
    @ac.describe(player="The player to give or remove bonus pay from")
    @ac.describe(points="The number of bonus pay to give or remove")
    async def bonuspay(self, interaction: Interaction, player: Member, points: int):
        # find the player by discord id
        player = self.session.query(Player).filter(Player.discord_id == player.id).first()
        if not player:
            await interaction.response.send_message("User doesn't have a Meta Campaign company", ephemeral=self.bot.use_ephemeral)
            return
        
        # update the player's bonus pay
        player.bonus_pay += points
        self.session.commit()
        logger.debug(f"User {player.name} now has {player.bonus_pay} bonus pay")
        await interaction.response.send_message(f"{player.name} now has {player.bonus_pay} bonus pay", ephemeral=self.bot.use_ephemeral)

    @ac.command(name="bulk_bonuspay", description="Give or remove a number of bonus pay from a set of players")
    @ac.describe(points="The number of bonus pay to give or remove")
    @ac.describe(status="Status of the unit (Inactive = 0, Active = 1, MIA = 2, KIA = 3)")
    async def bulk_bonus_pay(self, interaction: Interaction, status: str, points: int):
        # Find all units with corresponding Enum status
        status_enum = UnitStatus(status)
        units = self.session.query(Unit).filter(Unit.status == status_enum).all()
        for unit in units:
            # Find player of each unit and update their bonuspay
            player = unit.player
            player.bonus_pay += points
            logger.debug(f"User {player.name} now has {player.bonus_pay} requisition points")
        self.session.commit()
        await interaction.response.send_message(f"Players of units of the status {status} have received {points} bonus pay", ephemeral=self.bot.use_ephemeral)

    @ac.command(name="activateunits", description="Activate multiple units")
    async def activateunits(self, interaction: Interaction):
        # we need a modal with a paragraph input for the unit names
        # we identify if a newline is in the first 40 characters, if so, we split on that, otherwise we split on commas
        # we then check if the units exist, and if they do, we activate them
        # we send a response in the end saying which units were activated, and which ones didn't exist
        modal = Modal(title="Activate Units", custom_id="activate_units")
        modal.add_item(TextInput(label="Unit names", custom_id="unit_names", style=TextStyle.long))
        async def modal_callback(interaction: Interaction):
            unit_names = interaction.data["components"][0]["components"][0]["value"]
            logger.debug(f"Received unit names: {unit_names}")
            if "\n" in unit_names[:40]:
                unit_names = unit_names.split("\n")
            else:
                unit_names = unit_names.split(",")
            logger.debug(f"Parsed unit names: {unit_names}")
            activated = []
            not_found = []
            for unit_name in unit_names:
                unit = self.session.query(Unit).filter(Unit.name == unit_name).first()
                if unit:
                    activated.append(unit.name)
                    active_unit = ActiveUnit(unit_id=unit.id, player_id=unit.player_id)
                    self.session.add(active_unit)
                    logger.debug(f"Activated unit: {unit.name}")
                else:
                    not_found.append(unit_name)
                    logger.debug(f"Unit not found: {unit_name}")
                try:
                    self.session.commit()
                except Exception as e:
                    logger.error(f"Error committing to database: {e}")
                    await interaction.response.send_message(f"Error committing to database: {e}", ephemeral=self.bot.use_ephemeral)
            await interaction.response.send_message(f"Activated {activated}, not found {not_found}", ephemeral=self.bot.use_ephemeral)
            logger.debug(f"Activation results - Activated: {activated}, Not found: {not_found}")
        modal.on_submit = modal_callback
                
        await interaction.response.send_modal(modal)

    @ac.command(name="create_medal", description="Create a medal")
    @ac.describe(name="The name of the medal")
    @ac.describe(left_emote="The emote to use for the left side of the medal")
    @ac.describe(center_emote="The emote to use for the center of the medal")
    @ac.describe(right_emote="The emote to use for the right side of the medal")
    async def create_medal(self, interaction: Interaction, name: str, left_emote: str, center_emote: str, right_emote: str):
        # check if the emotes are valid
        _left_emote = self.bot.get_emoji(left_emote)
        _center_emote = self.bot.get_emoji(center_emote)
        _right_emote = self.bot.get_emoji(right_emote)
        if not (_left_emote and _center_emote and _right_emote):
            await interaction.response.send_message("Invalid emote", ephemeral=self.bot.use_ephemeral)
            return
        # create the medal
        self.bot.medal_emotes[name] = [left_emote, center_emote, right_emote]
        await interaction.response.send_message(f"Medal {name} created", ephemeral=self.bot.use_ephemeral)

    @ac.command(name="refresh_stats", description="Refresh the statistics and dossiers for all players")
    async def refresh_stats(self, interaction: Interaction):
        await interaction.response.send_message("Refreshing statistics and dossiers for all players", ephemeral=self.bot.use_ephemeral)
        for player in self.session.query(Player).all():
            await self.bot.queue.put((1, player)) # make the bot think the player was edited
        await interaction.followup.send("Refreshed statistics and dossiers for all players", ephemeral=self.bot.use_ephemeral)

bot: Bot = None
async def setup(_bot: Bot):
    global bot
    bot = _bot
    logger.info("Setting up Admin cog")
    await bot.add_cog(Admin(bot))
    await bot.tree.sync()

async def teardown():
    logger.info("Tearing down Admin cog")
    bot.remove_cog(Admin.__name__) # remove_cog takes a string, not a class
