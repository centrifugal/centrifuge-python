.PHONY: proto test lint

dev:
	pip install -r requirements.txt
	pip install -r requirements-dev.txt

proto:
	protoc --python_out=centrifuge/protocol client.proto

test:
	python -m unittest discover -s tests

lint:
	flake8 centrifuge --max-line-length 120 --exclude centrifuge/protocol/client_pb2.py
