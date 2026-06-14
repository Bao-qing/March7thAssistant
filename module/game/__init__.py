from module.config import cfg
from module.logger import log
from module.game.base import GameControllerBase
from module.game.cloud import CloudGameController
from module.game.local import LocalGameController
from module.game.reverse_cloud import ReverseCloudGameController

browser_cloud_game: CloudGameController = CloudGameController(cfg=cfg, logger=log)
reverse_cloud_game: ReverseCloudGameController = ReverseCloudGameController(cfg=cfg, logger=log)
cloud_game: GameControllerBase = reverse_cloud_game if cfg.get_value("cloud_game_adapter", "browser") == "reverse" else browser_cloud_game
local_game: LocalGameController = LocalGameController(cfg=cfg, logger=log)


def get_game_controller() -> GameControllerBase:
    if cfg.cloud_game_enable:
        if cfg.get_value("cloud_game_adapter", "browser") == "reverse":
            return reverse_cloud_game
        return browser_cloud_game
    else:
        local_game.reload_config(cfg)
        return local_game
