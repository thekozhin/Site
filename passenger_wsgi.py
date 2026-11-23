import sys
import os

# Правильный путь к виртуальному окружению
INTERP = os.path.expanduser("~/flaskenv/bin/python")
if sys.executable != INTERP:
    os.execl(INTERP, INTERP, *sys.argv)

# Добавляем путь к проекту
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Импортируем ваше Flask-приложение (переменная называется application)
from app import application
