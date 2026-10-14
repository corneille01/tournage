from workers import WorkerEntrypoint
from workers import asgi

from backend.db import configure_hyperdrive
from backend.main import app


class Default(WorkerEntrypoint):

    async def fetch(self, request):
        configure_hyperdrive(self.env.HYPERDRIVE)

        # Les routes sont d'abord envoyées à FastAPI.
        # Les fichiers statiques sont ensuite pris en charge
        # par Cloudflare Workers Static Assets.
        response = await asgi.fetch(
            app,
            request,
            self.env,
        )

        return response