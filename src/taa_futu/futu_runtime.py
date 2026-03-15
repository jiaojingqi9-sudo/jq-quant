from __future__ import annotations

import logging


def configure_futu_logging(futu_module) -> None:
    try:
        logger = futu_module.common.ft_logger.logger
    except AttributeError:
        return

    # Futu prints routine connect/disconnect lifecycle events to stdout.
    # Keep only real SDK errors on the console so the control panel stays readable.
    logger.console_level = logging.ERROR
