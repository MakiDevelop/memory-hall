from memory_hall.server.routes.admin import router as admin_router
from memory_hall.server.routes.health import router as health_router
from memory_hall.server.routes.memory import router as memory_router

__all__ = ["admin_router", "health_router", "memory_router"]
