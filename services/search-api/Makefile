IMAGE ?= akashweb/search-api
TAG   ?= 0.3.2

build:
	docker build -t $(IMAGE):$(TAG) .

push:
	docker push $(IMAGE):$(TAG)

all: build push
