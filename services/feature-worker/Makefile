IMAGE ?= akashweb/feature-worker
TAG   ?= 0.3.0

build:
	docker build -t $(IMAGE):$(TAG) .

push:
	docker push $(IMAGE):$(TAG)

all: build push
