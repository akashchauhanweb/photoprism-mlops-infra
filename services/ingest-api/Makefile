IMAGE ?= akashweb/ingest-api
TAG   ?= 0.1.3

build:
	docker build -t $(IMAGE):$(TAG) .

push:
	docker push $(IMAGE):$(TAG)

all: build push
