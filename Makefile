.PHONY: proto

proto:
	protoc --python_out=centrifuge/protocol client.proto
