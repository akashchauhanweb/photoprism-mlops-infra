IMAGE ?= akashweb/feedback-trainer
TAG   ?= 0.1.0

build:
	docker build --network host -f Dockerfile.feedback_train -t $(IMAGE):$(TAG) .

push:
	docker push $(IMAGE):$(TAG)

all: build push
