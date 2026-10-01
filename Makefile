build:
	python -m pip install --upgrade build
	python -m build

run:
	python main.py

publish-pypi:
	python -m pip install --upgrade twine
	python -m twine upload dist/*

publish-artifact-registry:
	python -m pip install --upgrade twine
	python -m twine upload --repository-url ${ARTIFACTORY_URL} dist/*

.PHONY: build run publish-pypi publish-artifact-registry
