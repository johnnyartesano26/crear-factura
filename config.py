import os
import sys
import logging
import logging.handlers
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

LOG_DIR = Path(os.getenv("Z1_LOG_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")))
LOG_LEVEL = os.getenv("Z1_LOG_LEVEL", "INFO").upper()
LOG_MAX_BYTES = int(os.getenv("Z1_LOG_MAX_BYTES", str(5 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("Z1_LOG_BACKUP_COUNT", "5"))
DEBUG_MODE = os.getenv("Z1_DEBUG", "").lower() in ("1", "true", "yes", "on")
PROFILE_MODE = os.getenv("Z1_PROFILE", "").lower() in ("1", "true", "yes", "on")


def setup_logging() -> logging.Logger:
    """Configura el logger raíz con rotación de archivos (info + error) y consola.

    Preserva las técnicas de depuración del z1-agent: niveles, timestamps,
    archivo rotativo y un archivo separado solo para errores. Al configurar el
    logger raíz, los logs de todos los módulos (facturacion, alegra_client_async)
    se capturan tanto en GitHub Actions (consola) como en archivos locales.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger()
    level = getattr(logging, LOG_LEVEL, logging.INFO)
    logger.setLevel(level)

    if not logger.handlers:
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        file_handler = logging.handlers.RotatingFileHandler(
            LOG_DIR / "facturacion.log",
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        error_handler = logging.handlers.RotatingFileHandler(
            LOG_DIR / "facturacion-error.log",
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(formatter)
        logger.addHandler(error_handler)

        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(level)
        console_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        logger.addHandler(console_handler)

    return logger


@contextmanager
def debug_on_error():
    """Si Z1_DEBUG=true, entra en pdb.post_mortem() ante una excepción no controlada."""
    try:
        yield
    except Exception:
        if DEBUG_MODE:
            import pdb
            pdb.post_mortem()
        raise


@contextmanager
def profile_if_enabled(sort_by: str = "cumtime"):
    """Si Z1_PROFILE=true, perfila el bloque con cProfile y muestra el top 30."""
    if PROFILE_MODE:
        import cProfile
        import pstats
        profiler = cProfile.Profile()
        profiler.enable()
        try:
            yield
        finally:
            profiler.disable()
            stats = pstats.Stats(profiler)
            stats.sort_stats(sort_by)
            stats.print_stats(30)
    else:
        yield


def timeit(func):
    """Decorador que mide el tiempo de una función (sync o async) cuando Z1_PROFILE=true."""
    import functools
    import inspect
    import time

    @functools.wraps(func)
    def sync_wrapper(*args, **kwargs):
        if not PROFILE_MODE:
            return func(*args, **kwargs)
        start = time.perf_counter()
        result = func(*args, **kwargs)
        elapsed = time.perf_counter() - start
        logging.getLogger(func.__module__).debug("[perf] %s tomó %.4fs", func.__name__, elapsed)
        return result

    @functools.wraps(func)
    async def async_wrapper(*args, **kwargs):
        if not PROFILE_MODE:
            return await func(*args, **kwargs)
        start = time.perf_counter()
        result = await func(*args, **kwargs)
        elapsed = time.perf_counter() - start
        logging.getLogger(func.__module__).debug("[perf] %s tomó %.4fs", func.__name__, elapsed)
        return result

    if inspect.iscoroutinefunction(func):
        return async_wrapper
    return sync_wrapper
