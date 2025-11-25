#!/bin/bash
# Запуск API в фоні
python dtek_api.py &
API_PID=$!

# Трохи почекати поки API запуститься
sleep 5

# Запуск основного бота
python Bot.py

# При зупинці бота - зупинити і API
kill $API_PID
