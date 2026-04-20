IMAGE ?= akashweb/search-api
TAG   ?= 0.1.1

build:
	docker build -t $(IMAGE):$(TAG) .

push:
	docker push $(IMAGE):$(TAG)

all: build push
