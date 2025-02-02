import datetime
import inspect
import json
import logging
import logging.config
import os
import pathlib
from types import ModuleType
from typing import Any, Callable, ContextManager, List, Optional, Union

import dotenv
import orjson  # type: ignore
import sentry_sdk
import structlog
import platform
import tempfile

from structlog_sentry_logger import structlog_sentry

ROOT_DIR = pathlib.Path("/tmp" if platform.system() == "Darwin" else tempfile.gettempdir())
LOG_DATA_DIR = ROOT_DIR / ".logs"
LOG_DATA_DIR.mkdir(exist_ok=True)
DATETIME_FORMAT = "iso"
_CONFIGS = {"USE_ORJSON": True}


def _toggle_json_library(use_orjson: bool = True) -> None:
    _CONFIGS["USE_ORJSON"] = use_orjson


def get_namespaced_module_name(__file__: Union[pathlib.Path, str]) -> str:
    fully_qualified_path = pathlib.Path(__file__).resolve()
    prefix_dir = str(ROOT_DIR) if str(ROOT_DIR) in str(fully_qualified_path) else "/"
    namespaces = fully_qualified_path.relative_to(prefix_dir).with_suffix("").parts
    return ".".join(namespaces)


def get_caller_name(prev_stack_frame: inspect.FrameInfo) -> str:
    deduced_calling_module = deduce_module(prev_stack_frame)
    return (
        deduced_calling_module.__name__
        if deduced_calling_module
        and not is_caller_main(deduced_calling_module.__name__)
        else get_namespaced_module_name(prev_stack_frame.filename)
    )


def deduce_module(prev_stack_frame: inspect.FrameInfo) -> Optional[ModuleType]:
    return inspect.getmodule(prev_stack_frame[0])


def get_caller_name_from_frames(stack_frames: List[inspect.FrameInfo]) -> str:
    prev_stack_frame = stack_frames[1] if __file__.endswith(".py") else stack_frames[0]
    return get_caller_name(prev_stack_frame)


def get_logger(name: Optional[str] = None) -> Any:
    """
    Convenience function that returns a logger

    Returns: A proxy that creates a correctly configured logger bound to
    the __name__ of the calling module

    """
    del name
    stack_frames = inspect.stack()
    caller_name = get_caller_name_from_frames(stack_frames)
    if not structlog.is_configured():
        timestamper = structlog.processors.TimeStamper(fmt=DATETIME_FORMAT)
        set_logging_config(caller_name, timestamper)
        set_structlog_config(timestamper)
    logger = structlog.get_logger(caller_name)
    logger.setLevel(logging.DEBUG)
    return logger


getLogger = get_logger
"""
CamelCase alias for `structlog_sentry_logger.get_logger`.
"""


def get_config_dict() -> dict:
    """
    Convenience function to get the local logging configuration dictionary,
    e.g., to help configure loggers from other libraries.

    Returns: The logging configuration dictionary that would be used to
    configure the Python logging library component of the logger

    """
    stack_frames = inspect.stack()
    caller_name = get_caller_name_from_frames(stack_frames)
    timestamper = structlog.processors.TimeStamper(fmt=DATETIME_FORMAT)
    return get_logging_config(caller_name, timestamper)


def is_caller_main(caller_name: str) -> bool:
    return caller_name == "__main__"


def get_logging_config(
    module_name: str, timestamper: structlog.processors.TimeStamper
) -> dict:
    handlers = get_handlers(module_name)
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": (get_formatters(timestamper)),
        "handlers": handlers,
        "loggers": {
            "": {
                "handlers": list(handlers.keys()),
                "level": "WARNING",
                "propagate": True,
            }
        },
    }


def set_logging_config(
    module_name: str, timestamper: structlog.processors.TimeStamper
) -> None:
    config_dict = get_logging_config(module_name, timestamper)
    logging.config.dictConfig(config_dict)


def get_formatters(timestamper: structlog.processors.TimeStamper) -> dict:
    pre_chain = [
        # Add the log level and a timestamp to the event_dict if the log
        # entry is not from structlog.
        structlog.stdlib.add_log_level,
        timestamper,
        structlog.stdlib.add_logger_name,
    ]
    return {
        "plain": {
            "()": structlog.stdlib.ProcessorFormatter,
            "processor": structlog.processors.JSONRenderer(
                serializer=serializer,
                option=orjson.OPT_NON_STR_KEYS | orjson.OPT_SORT_KEYS,
            ),
            "foreign_pre_chain": pre_chain,
        },
        "colored": {
            "()": structlog.stdlib.ProcessorFormatter,
            "processor": structlog.dev.ConsoleRenderer(colors=True),
            "format": "%(message)s [in %(funcName)s]",
            "foreign_pre_chain": pre_chain,
        },
    }


def serializer(
    *args: Any,
    default: Optional[Callable[[Any], Any]] = None,
    option: Optional[int] = orjson.OPT_NON_STR_KEYS | orjson.OPT_SORT_KEYS,
) -> str:
    if _CONFIGS["USE_ORJSON"]:
        return orjson.dumps(*args, default=default, option=option).decode()  # type: ignore[misc]
    return json.dumps(*args, sort_keys=True)


def get_handlers(module_name: str) -> dict:
    default_key = "default"
    base_handlers = {
        default_key: {
            "level": "DEBUG",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
        }
    }
    if _ENV_VARS_REQUIRED_BY_LIBRARY[get_handlers] in os.environ:
        # Prettify stdout/stderr streams
        base_handlers[default_key]["formatter"] = "colored"
        # Add filename handler
        file_timestamp = datetime.datetime.utcnow().isoformat().replace(":", "-")
        log_file_name = f"{file_timestamp}_{module_name}.jsonl"
        log_file_path = LOG_DATA_DIR / log_file_name
        base_handlers["filename"] = {
            "level": "DEBUG",
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(log_file_path),
            # 1 MB
            "maxBytes": 1 << 20,  # type: ignore[dict-item]
            "backupCount": 3,  # type: ignore[dict-item]
            "formatter": "plain",
        }
    else:
        base_handlers[default_key]["formatter"] = "plain"
    return base_handlers


def set_structlog_config(timestamper: structlog.processors.TimeStamper) -> None:
    structlog_processors = [
        timestamper,
        structlog.processors.StackInfoRenderer(),
        add_severity_field_from_level_if_in_cloud_environment,
    ]
    stdlib_log_compatibility_processors = [
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.PositionalArgumentsFormatter(),
        SentryBreadcrumbJsonProcessor(level=logging.ERROR, tag_keys="__all__"),
    ]

    # Note: MUST come last!
    format_wrapper_processer = [structlog.stdlib.ProcessorFormatter.wrap_for_formatter]
    structlog.configure(
        processors=(
            stdlib_log_compatibility_processors  # type: ignore[arg-type]
            + structlog_processors
            + format_wrapper_processer  # type: ignore[arg-type,operator]
        ),
        # See [Performance](https://www.structlog.org/en/stable/performance.html)
        # for an in-depth explanation of the below settings
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def add_severity_field_from_level_if_in_cloud_environment(
    logger: Any,  # pylint: disable=unused-argument
    method: str,  # pylint: disable=unused-argument
    event_dict: structlog.types.EventDict,
) -> structlog.types.EventDict:
    """A custom processor for structlog for Cloud Logging compatibility

    Since Cloud Logging infers log levels from the `severity` key, simply duplicates
    `level` to the `severity` field in the logger's event dictionary.
    """

    if (
        is_cloud_logging_compatibility_mode_requested()
        or is_probably_in_cloud_environment()
    ):
        cloud_logging_log_level_key, python_log_level_key = "severity", "level"
        if cloud_logging_log_level_key in event_dict:
            # Dogfood by instantiating a local logger with own library.
            # Note: NO infinite loop since the below log message does *NOT* use
            # `severity` as a key in the emitted event.
            local_logger = get_logger()
            local_logger.warning(
                "Existing log value being overwritten",
                src_key=python_log_level_key,
                dest_key=cloud_logging_log_level_key,
                old_value=event_dict[cloud_logging_log_level_key],
                new_value=event_dict[python_log_level_key],
                logger_name=logger.name,
            )
        event_dict[cloud_logging_log_level_key] = event_dict[python_log_level_key]
    return event_dict


def is_cloud_logging_compatibility_mode_requested() -> bool:
    return (
        _ENV_VARS_REQUIRED_BY_LIBRARY[is_cloud_logging_compatibility_mode_requested]
        in os.environ
    )


def is_probably_in_cloud_environment() -> bool:
    """Returns True if it is *likely* (but not guaranteed) logging is occurring in the context of a Cloud Logging environment"""
    for env_var in [
        # GKE
        # There are no GKE-specific environment variable that definitively imply we are
        # running in GKE... Falling back to detecting Kubernetes-injected environment
        # variables since those are the only ones present in GKE pods that *could* imply
        # we are running in GKE.
        # Kubernetes
        # see: https://kubernetes.io/docs/concepts/services-networking/connect-applications-service/#environment-variables
        "KUBERNETES_SERVICE_HOST",
        # Cloud Function
        # see: https://cloud.google.com/functions/docs/configuring/env-var#runtime_environment_variables_set_automatically
        "GCP_PROJECT",
        # GAE
        # see: https://cloud.google.com/functions/docs/configuring/env-var#runtime_environment_variables_set_automatically
        "GOOGLE_CLOUD_PROJECT",
    ]:
        if env_var in os.environ:
            return True
    return False


_ENV_VARS_REQUIRED_BY_LIBRARY = {
    get_handlers: "STRUCTLOG_SENTRY_LOGGER_LOCAL_DEVELOPMENT_LOGGING_MODE_ON",
    is_cloud_logging_compatibility_mode_requested: "STRUCTLOG_SENTRY_LOGGER_CLOUD_LOGGING_COMPATIBILITY_MODE_ON",
    sentry_sdk.init: "SENTRY_DSN",
}


class SentryBreadcrumbJsonProcessor(structlog_sentry.SentryJsonProcessor):

    """
    Addresses: `SentryJsonProcessor breaks logging breadcrumbs #25`_
    (source_)

    .. _`SentryJsonProcessor breaks logging breadcrumbs #25`: https://github.com/kiwicom/structlog-sentry/issues/25
    .. _`source`: https://github.com/kiwicom/structlog-sentry/issues/25#issuecomment-660292563
    """

    def __init__(  # pylint: disable=too-many-arguments
        self,
        breadcrumb_level: int = logging.INFO,
        level: int = logging.WARNING,
        active: bool = True,
        as_extra: bool = True,
        tag_keys: Union[List[str], str] = None,
    ) -> None:
        self.breadcrumb_level = breadcrumb_level
        super().__init__(
            level=level, active=active, as_extra=as_extra, tag_keys=tag_keys
        )

    @staticmethod
    def save_breadcrumb(logger: Any, event_dict: structlog.types.EventDict) -> None:
        data = event_dict.copy()  # type: ignore[attr-defined]
        data.pop("event")
        data.pop("logger", None)
        data.pop("level", None)
        data.pop("timestamp", None)
        breadcrumb = {
            "ty": "log",
            "level": event_dict["level"].lower(),
            "category": event_dict.get("logger") or logger.name,
            "message": event_dict["event"],
            "data": data,
        }
        sentry_sdk.add_breadcrumb(breadcrumb, hint={"event_dict": event_dict})

    def __call__(
        self, logger: Any, method: str, event_dict: structlog.types.EventDict
    ) -> structlog.types.EventDict:
        do_breadcrumb = (
            getattr(logging, event_dict["level"].upper()) >= self.breadcrumb_level
        )

        if do_breadcrumb:
            self.save_breadcrumb(logger, event_dict)

        return super().__call__(logger=logger, method=method, event_dict=event_dict)


def _load_library_specific_env_vars() -> None:
    # Inject into the environment ONLY the env vars required by the library;
    # we manually update/add to the the environment ONLY the keys in a user's `.env` for
    # which the library is inspecting (i.e., the set intersection between the
    # aforementioned), and only if they weren't already defined in the environment.
    users_dotenv_values = dotenv.dotenv_values(dotenv.find_dotenv())
    legal_env_vars_keys = (
        _ENV_VARS_REQUIRED_BY_LIBRARY.values() & users_dotenv_values.keys()
    )

    for k in legal_env_vars_keys:
        v = users_dotenv_values[k]
        # Any env-var-to-add already defined in the environment will take precedent over
        # what is defined in a user's `.env` file.
        if k not in os.environ and v is not None:
            os.environ[k] = v


def _init_sentry() -> ContextManager[Any]:
    # Note: if DSN isn't defined, will silently not transmit telemetry
    return sentry_sdk.init()  # pylint: disable=abstract-class-instantiated
