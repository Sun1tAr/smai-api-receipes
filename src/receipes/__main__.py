import uvicorn

from receipes.config import Settings
from receipes.main import create_app


def main() -> None:
    settings = Settings()
    uvicorn.run(
        create_app(settings),
        # Внутри контейнера нужен 0.0.0.0; наружу порт публикует только Compose.
        host="0.0.0.0",
        port=8000,
        # Логирование настроено в create_app; HTTP-события пишет middleware.
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
