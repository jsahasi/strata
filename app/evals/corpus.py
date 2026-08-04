"""The corpus and its oracle, loaded from disk.

No database, no model, no network, no clock. The harness reads three text files
and one JSON file and computes everything else, so a reviewer can run it on a
clean checkout with nothing installed but the requirements and no API key set.

`data/manifest.json` is the oracle, and it is worth saying why it can be
trusted. Its offsets were produced by a separate script and re-verified by
reading the bytes back at each offset -- see `data/README.md`. Nothing in
`app/` computed them. That independence is the whole value: a scorecard whose
expected answers came from the code under test measures only that the code
agrees with itself.

Files are read in binary and decoded, never opened in text mode. The manifest's
offset convention is Python string indexing over UTF-8 with the end exclusive,
and text mode would translate newlines on some platforms and move every offset
after the first one.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from app.diff.engine import PassageRef

# Imported rather than copied, per best-practices.html §27: the eval must split
# passages exactly as ingestion does, or it scores a pipeline the product does
# not run. The leading underscore is ingestion's own; this is the single place
# outside that module which depends on it, and it is named here so a rename is
# caught by this harness rather than by a reviewer.
from app.ingestion.ingest import _segment

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"


@dataclass(frozen=True)
class CitationSite:
    """One offset the manifest records, ready to be handed to the verifier.

    `expected_occurrence` is None for a quote the manifest records once and
    an index for one of the nine repeated boilerplate spans. That index comes
    from the manifest's own ordering, not from the verifier, so the check is
    an independent comparison rather than the code confirming itself.
    """

    label: str
    version_id: str
    start: int
    end: int
    quoted_text: str
    expected_occurrence: int | None = None
    section: str | None = None

    @property
    def where(self) -> str:
        return f"{self.version_id}:{self.start}:{self.end}"


class Corpus:
    """Everything the scorecard reads, loaded once."""

    def __init__(self, data_dir: Path, manifest: dict, texts: dict[str, str]):
        self.data_dir = data_dir
        self.manifest = manifest
        self._texts = texts

    @classmethod
    def load(cls, data_dir: Path | str | None = None) -> "Corpus":
        directory = Path(data_dir) if data_dir is not None else DEFAULT_DATA_DIR
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(
                f"no manifest at {manifest_path}. The eval harness reads the "
                "committed corpus; point --data at a directory holding "
                "manifest.json and the version files."
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        texts = {
            version["id"]: (directory / version["file"]).read_bytes().decode("utf-8")
            for version in manifest["versions"]
        }
        return cls(directory, manifest, texts)

    # ---- versions -------------------------------------------------------

    @property
    def version_ids(self) -> list[str]:
        return [version["id"] for version in self.manifest["versions"]]

    def text(self, version_id: str) -> str:
        return self._texts[version_id]

    def status(self, version_id: str) -> str:
        """DRAFT or FINAL, read from the manifest field. Never inferred (ADR-005)."""
        for version in self.manifest["versions"]:
            if version["id"] == version_id:
                return version["status"]
        raise KeyError(f"no version {version_id!r} in the manifest")

    def label(self, version_id: str) -> str:
        for version in self.manifest["versions"]:
            if version["id"] == version_id:
                return version["label"]
        raise KeyError(f"no version {version_id!r} in the manifest")

    def digest(self, version_id: str) -> str:
        """Which bytes this run scored. A verdict belongs to a version (§28)."""
        raw = self._texts[version_id].encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:12]

    def passages(self, version_id: str) -> list[PassageRef]:
        """The version split the way ingestion splits it, with exact offsets."""
        return [
            PassageRef(version_id, start, end, chunk)
            for start, end, chunk in _segment(self._texts[version_id])
        ]

    # ---- labelled changes -----------------------------------------------

    @property
    def changes(self) -> list[dict]:
        return list(self.manifest["changes"])

    def change(self, change_id: str) -> dict:
        for change in self.manifest["changes"]:
            if change["id"] == change_id:
                return change
        raise KeyError(f"no change {change_id!r} in the manifest")

    # ---- the repeated boilerplate ---------------------------------------

    @property
    def boilerplate_sentence(self) -> str:
        return self.manifest["repeated_boilerplate"]["sentence"]

    def boilerplate_by_version(self) -> dict[str, list[dict]]:
        """The nine spans, grouped by version and ordered by position.

        Order is the manifest's, established by sorting its own offsets. The
        occurrence index a citation must state is this position, so it is
        derived here and never asked of the code being scored.
        """
        grouped: dict[str, list[dict]] = {}
        for occurrence in self.manifest["repeated_boilerplate"]["occurrences"]:
            grouped.setdefault(occurrence["version"], []).append(occurrence)
        return {
            version_id: sorted(spans, key=lambda span: span["start"])
            for version_id, spans in grouped.items()
        }

    def boilerplate_sites(self) -> list[CitationSite]:
        sites: list[CitationSite] = []
        for version_id, spans in self.boilerplate_by_version().items():
            for index, span in enumerate(spans):
                sites.append(
                    CitationSite(
                        label=f"boilerplate {version_id}[{index}]",
                        version_id=version_id,
                        start=span["start"],
                        end=span["end"],
                        quoted_text=self.boilerplate_sentence,
                        expected_occurrence=index,
                        section=span.get("section"),
                    )
                )
        return sites

    # ---- every offset the manifest records ------------------------------

    def citation_sites(self) -> list[CitationSite]:
        """Each offset pair the manifest states, in manifest order.

        Three keys carry offsets on a change: the text before, the text after,
        and -- where the manifest records that a v2 wording survived into v3 --
        the unchanged third copy. CHG-4 has no before side at all, and the
        absence is the point, so it contributes no site here and is checked by
        the draft-versus-final metric instead.
        """
        sites: list[CitationSite] = []
        for change in self.manifest["changes"]:
            for key in ("before", "after", "also_present_unchanged_in"):
                side = change.get(key)
                if not side or side.get("version") is None:
                    continue
                sites.append(
                    CitationSite(
                        label=f"{change['id']} {key}",
                        version_id=side["version"],
                        start=side["start"],
                        end=side["end"],
                        quoted_text=side["exact_text"],
                        section=change.get("section"),
                    )
                )
        return sites + self.boilerplate_sites()
