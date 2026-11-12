from .web_controller import WebController

async def setup(bot):
    cog = WebController(bot)
    await bot.add_cog(cog)
