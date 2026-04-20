IMAGE ?= akashweb/clip-api
TAG   ?= 0.1.1

build:
	docker build --network host -t $(IMAGE):$(TAG) .

push:
	docker push $(IMAGE):$(TAG)

all: build push
