.PHONY: install index service bot
install:
	pip install -r requirements.txt

index:
	python scripts/build_index.py

service:
	python -m app.service

bot:
	python -m bot.max_bot
