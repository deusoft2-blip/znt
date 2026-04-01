# -*- coding: utf-8 -*-
########################
#    Sokolov Dmitry    #
# xx.sokolov@gmail.com #
#  https://t.me/ZbxNTg #
########################
# https://github.com/xxsokolov/znt
from typing import Union
from fastapi import Depends, HTTPException, APIRouter, Path, Query, BackgroundTasks
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from app.databases.database import SessionLocal, engine
from app.api import schemas, models, cruds
from app.services.telegram.interactive_bot import InteractiveBot
from app import config, logger

models.bot.Base.metadata.create_all(bind=engine)

interactive_router = APIRouter()

# Хранилище активных ботов
active_bots: dict[int, InteractiveBot] = {}


# Dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@interactive_router.get("/interactive", response_model=list[schemas.bot.FullBot], summary="Найти бота для интерактивного режима")
def find_interactive_bot(
        id: Union[int, None] = None,
        name: Union[str, None] = Query(None, regex="^(?=.{5,35}$)@[a-zA-Z0-9_]+(?:bot|Bot)"),
        db: Session = Depends(get_db)) -> list[list]:
    """Поиск бота для использования в интерактивном режиме"""
    find_bot = cruds.bot.find_bot(db, bot_id=id, name=name)
    if find_bot is None:
        raise HTTPException(status_code=404, detail="Бот {bot} не найден".format(bot=name))
    return find_bot


@interactive_router.post("/interactive/start/{bot_id}", summary="Запустить интерактивного бота")
def start_interactive_bot(
        bot_id: int = Path(..., example='1', description="Укажите ид бота."),
        background_tasks: BackgroundTasks = None,
        db: Session = Depends(get_db)):
    """Запуск интерактивного Telegram бота для просмотра метрик Zabbix"""
    
    # Проверяем существование бота
    bot_record = cruds.bot.find_bot(db, bot_id=bot_id)
    if not bot_record:
        raise HTTPException(status_code=404, detail="Бот с ид {id} не найден".format(id=bot_id))
    
    bot_data = bot_record[0]
    
    # Проверяем, не запущен ли уже бот
    if bot_id in active_bots:
        return JSONResponse(content={
            "status": "warning",
            "detail": f"Бот {bot_data.name} уже запущен"
        })
    
    # Получаем настройки прокси если есть
    proxy_use = False
    proxy = None
    
    if bot_data.proxy_id:
        proxy_record = cruds.proxy.find_proxy(db, proxy_id=bot_data.proxy_id)
        if proxy_record:
            proxy_data = proxy_record[0]
            proxy_use = proxy_data.active
            proxy = f"{proxy_data.server}:{proxy_data.port}"
    
    # Создаем и запускаем бота
    interactive_bot = InteractiveBot(
        token=bot_data.token,
        proxy=proxy,
        proxy_use=proxy_use
    )
    
    try:
        # Запускаем бота в фоновом режиме
        if background_tasks:
            background_tasks.add_task(interactive_bot.start_bot)
        else:
            # Если background_tasks не передан, запускаем напрямую
            import threading
            thread = threading.Thread(target=interactive_bot.start_bot)
            thread.daemon = True
            thread.start()
        
        active_bots[bot_id] = interactive_bot
        
        logger.log.info(f"Интерактивный бот {bot_data.name} запущен")
        
        return JSONResponse(content={
            "status": "success",
            "detail": f"Интерактивный бот {bot_data.name} запущен"
        })
        
    except Exception as e:
        logger.log.error(f"Ошибка запуска интерактивного бота: {str(e)}", exc_info=config.getboolean('logging', 'exc_info'))
        raise HTTPException(status_code=500, detail=f"Ошибка запуска бота: {str(e)}")


@interactive_router.post("/interactive/stop/{bot_id}", summary="Остановить интерактивного бота")
def stop_interactive_bot(
        bot_id: int = Path(..., example='1', description="Укажите ид бота."),
        db: Session = Depends(get_db)):
    """Остановка интерактивного Telegram бота"""
    
    # Проверяем, запущен ли бот
    if bot_id not in active_bots:
        raise HTTPException(status_code=400, detail="Бот с ид {id} не запущен".format(id=bot_id))
    
    try:
        interactive_bot = active_bots[bot_id]
        interactive_bot.stop_bot()
        del active_bots[bot_id]
        
        bot_record = cruds.bot.find_bot(db, bot_id=bot_id)
        bot_name = bot_record[0].name if bot_record else f"бот #{bot_id}"
        
        logger.log.info(f"Интерактивный бот {bot_name} остановлен")
        
        return JSONResponse(content={
            "status": "success",
            "detail": f"Интерактивный бот {bot_name} остановлен"
        })
        
    except Exception as e:
        logger.log.error(f"Ошибка остановки интерактивного бота: {str(e)}", exc_info=config.getboolean('logging', 'exc_info'))
        raise HTTPException(status_code=500, detail=f"Ошибка остановки бота: {str(e)}")


@interactive_router.get("/interactive/status", summary="Статус интерактивных ботов")
def get_interactive_bots_status():
    """Получение статуса всех запущенных интерактивных ботов"""
    
    status_list = []
    for bot_id, bot_instance in active_bots.items():
        status_list.append({
            "bot_id": bot_id,
            "status": "running",
            "updater_active": bot_instance.updater is not None
        })
    
    return JSONResponse(content={
        "total": len(status_list),
        "bots": status_list
    })
