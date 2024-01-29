from __future__ import annotations

import json
import os
import re
import shutil
import zipfile
from contextlib import contextmanager
from functools import cached_property
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Generator, List, Optional

from docs.docs_version_bucket_info import S3_URL
from docs.prepare_prior_versions import (
    prepare_prior_version,
    prepare_prior_versions,
    Version,
)
from docs.logging import Logger

if TYPE_CHECKING:
    from invoke.context import Context


class DocsBuilder:
    def __init__(
        self,
        context: Context,
        current_directory: Path,
        is_pull_request: bool,
        is_local: bool,
    ) -> None:
        self._context = context
        self._current_directory = current_directory
        self._is_pull_request = is_pull_request
        self._is_local = is_local

        self._current_commit = self._run_and_get_output("git rev-parse HEAD")
        self._current_branch = self._run_and_get_output(
            "git rev-parse --abbrev-ref HEAD"
        )

    def build_docs(self) -> None:
        """Build API docs + docusaurus docs.

        NOTE: This will replace `build_docs` very shortly!
        """
        self.logger.print_header("Preparing to build docs...")
        self._load_all_versioned_docs()

        self._invoke_api_docs()

        self.logger.print_header("Building docusaurus docs...")
        self._context.run("yarn build")

    def build_docs_locally(self) -> None:
        """Serv docs locally."""
        self._prepare()
        self.logger.print_header("Running yarn start to serve docs locally...")
        self._context.run("yarn start")

    def create_version(self, version: Version) -> None:
        self.logger.print_header(f"Creating version {version}")
        MIN_PYTHON_VERSION = 3.8
        MAX_PYTHON_VERSION = 3.8

        # load state of code for given version and process it
        # we'll end up checking this branch out as well, but need the data in versioned_code for prepare_prior_version
        versions = self._load_all_versioned_docs()
        if version in versions:
            raise Exception(f"Version {version} already exists")

        # switch to the version branch for its docs and create versioned docs
        self._context.run(f"git checkout {version}")
        old_version_file = self._read_prior_release_version_file()
        self._write_release_version(
            "\n".join(
                [
                    "// this file is autogenerated",
                    "export default {",
                    f"  release_version: 'great_expectations, version {version}',",
                    f"   min_python: '{MIN_PYTHON_VERSION}',",
                    f"   max_python: '{MAX_PYTHON_VERSION}',",
                    "}",
                ]
            )
        )

        # create versioned_docs and load versioned_code
        self._context.run(f"yarn docusaurus docs:version {version}")
        self._load_versioned_code(version)

        # process the above
        os.chdir("..")  # TODO: none of this messing with current directory stuff
        prepare_prior_version(version)
        os.chdir("docusaurus")

        output_file = "oss_docs_versions.zip"
        self._context.run(
            f"zip -r {output_file} versioned_code versioned_docs versioned_sidebars versions.json"
        )
        self.logger.print(f"Created {output_file}")

        # restore version file and go back to intended branch
        self._write_release_version(old_version_file)
        self._context.run("git checkout -")

        # finally, check that we can actually build the docs
        self.logger.print_header("Testing that we can build the docs...")
        # this is the steps from build_docs minus loading data from s3
        self._invoke_api_docs()
        self._context.run("yarn build")
        self.logger.print_header(
            f"Successfully created version {version}. Upload {output_file} to S3."
        )

    def _prepare(self) -> None:
        """A whole bunch of common work we need"""
        self.logger.print_header("Preparing to build docs...")
        versions_loaded = self._load_files()

        self.logger.print_header(
            "Updating versioned code and docs via prepare_prior_versions.py..."
        )
        # TODO: none of this messing with current directory stuff
        os.chdir("..")
        prepare_prior_versions(versions_loaded)
        os.chdir("docusaurus")
        self.logger.print("Updated versioned code and docs")

        self._invoke_api_docs()
        self._checkout_correct_branch()

    @contextmanager
    def _load_zip(self, url: str) -> Generator[zipfile.ZipFile, None, None]:
        import requests  # imported here to avoid this getting imported before `invoke deps` finishes

        response = requests.get(url)
        zip_data = BytesIO(response.content)
        with zipfile.ZipFile(zip_data, "r") as zip_ref:
            yield zip_ref

    def _load_files(self) -> List[Version]:
        """Load oss_docs_versions zip and relevant versions from github.

        oss_docs_versions contains the versioned docs to be used later by prepare_prior_versions, as well
        as the versions.json file, which contains the list of versions that we then download from github.

        Returns a list of verions loaded.
        """
        versions = self._load_all_versioned_docs()
        for version in versions:
            self._load_versioned_code(version)
        return versions

    def _load_all_versioned_docs(self) -> List[Version]:
        self.logger.print(f"Copying previous versioned docs from {S3_URL}")
        if os.path.exists("versioned_code"):
            shutil.rmtree("versioned_code")
        os.mkdir("versioned_code")
        with self._load_zip(S3_URL) as zip_ref:
            zip_ref.extractall(self._current_directory)
            versions_json = zip_ref.read("versions.json")
            return [Version.from_string(x) for x in json.loads(versions_json)]

    def _load_versioned_code(self, version: Version) -> None:
        self.logger.print(
            f"Copying code referenced in docs from {version} and writing to versioned_code/version-{version}"
        )
        url = f"https://github.com/great-expectations/great_expectations/archive/refs/tags/{version}.zip"

        with self._load_zip(url) as zip_ref:
            zip_ref.extractall(self._current_directory / "versioned_code")
            old_location = (
                self._current_directory / f"versioned_code/great_expectations-{version}"
            )
            new_location = self._current_directory / f"versioned_code/version-{version}"
            shutil.move(str(old_location), str(new_location))

    def _invoke_api_docs(self) -> None:
        """Invokes the invoke api-docs command.
        If this is a non-PR running on netlify, we use the latest tag. Otherwise, we use the current branch.
        """
        self.logger.print("Invoking api-docs...")

        # TODO: not this: we should do this all in python
        self._run("(cd ../../; invoke api-docs)")

    def _checkout_correct_branch(self) -> None:
        """Ensure we are on the right branch to run docusaurus."""
        if self._is_local:
            self.logger.print_header(
                f"Building locally - Checking back out current branch ({self._current_branch}) before building the rest of the docs."
            )
            self._run(f"git checkout {self._current_branch}")
        else:
            self.logger.print_header(
                f"In a pull request or deploying in netlify (PULL_REQUEST = ${self._is_pull_request}) Checking out ${self._current_commit}."
            )
            self._run(f"git checkout {self._current_commit}")

    def _read_prior_release_version_file(self) -> str:
        with open(self._release_version_file, "r") as file:
            return file.read()

    def _write_release_version(self, content: str) -> None:
        with open(self._release_version_file, "w") as file:
            file.write(content)

    def _run(self, command: str) -> Optional[str]:
        result = self._context.run(command, echo=True)
        if not result:
            return None
        elif not result.ok:
            raise Exception(f"Failed to run command: {command}")
        return result.stdout.strip()

    def _run_and_get_output(self, command: str) -> str:
        output = self._run(command)
        assert output
        return output

    @property
    def _release_version_file(self) -> str:
        return "./docs/components/_data.jsx"

    @cached_property
    def _latest_tag(self) -> str:
        tags_string = self._run("git tag")
        assert tags_string is not None
        tags = [t for t in tags_string.split() if self._tag_regex.match(t)]
        return sorted(tags)[-1]

    @cached_property
    def logger(self) -> Logger:
        return Logger()

    @cached_property
    def _tag_regex(self) -> re.Pattern:
        return re.compile(r"([0-9]+\.)+[0-9]+")