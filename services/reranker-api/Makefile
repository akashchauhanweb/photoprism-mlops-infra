IMAGE ?= akashweb/reranker-api
TAG   ?= 0.1.6

build:
	docker build --network host -t $(IMAGE):$(TAG) .

push:
	docker push $(IMAGE):$(TAG)

all: build push
