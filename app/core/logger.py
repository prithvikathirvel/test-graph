import os
import logging
from logging.handlers import TimedRotatingFileHandler
from contextvars import ContextVar

# 🚀 Context Variable: This allows us to track async requests across all files!
# It defaults to "SYSTEM" but will be updated to the "thread_id" during LangGraph executions.
trace_ctx: ContextVar[str] = ContextVar("trace_ctx", default="SYSTEM")

class TraceInjectingFilter(logging.Filter):
    """Injects the async context variable into the log record."""
    def filter(self, record):
        record.trace_ctx = trace_ctx.get()
        return True

def setup_logging():
    """Initializes production-ready logging."""
    
    # 1. Ensure logs directory exists
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "app.log")

    # 2. Define the Log Format (Highly Traceable)
    # Output: 2026-04-05 10:00:00 | INFO | [thread_123abc] | app.nodes.agents | Entering LLM...
    log_format = "%(asctime)s | %(levelname)-7s | [%(trace_ctx)s] | %(name)s | %(message)s"
    formatter = logging.Formatter(log_format, datefmt="%Y-%m-%d %H:%M:%S")

    # 3. File Handler: Daily rotation, keep exactly 7 days of logs
    file_handler = TimedRotatingFileHandler(
        filename=log_file,
        when="midnight",     # Rotate at midnight
        interval=1,          # Every 1 day
        backupCount=7,       # Auto-delete files older than 7 days!
        encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(TraceInjectingFilter())

    # 4. Console Handler: Print to terminal (PM2)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(TraceInjectingFilter())

    # 5. Configure the Root Logger
    root_logger = logging.getLogger()
    
    # Check environment variable for log level (Default to INFO)
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    root_logger.setLevel(getattr(logging, log_level, logging.INFO))
    
    # Remove existing handlers to prevent duplicate logs if re-initialized
    if root_logger.hasHandlers():
        root_logger.handlers.clear()
        
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    # 6. Silence excessively noisy 3rd-party libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("pymongo").setLevel(logging.WARNING)