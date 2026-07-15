from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LanguageSpec:
    id: str
    label: str
    extensions: tuple[str, ...]
    support_level: str
    parser: str | None = None
    automatic_fix: bool = False

    @property
    def analyzable(self) -> bool:
        return self.parser is not None


LANGUAGE_SPECS: tuple[LanguageSpec, ...] = (
    LanguageSpec("python", "Python", (".py",), "full", "python", True),
    LanguageSpec("c", "C", (".c",), "full", "c"),
    LanguageSpec("cpp", "C++ / Qt", (".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"), "full", "cpp"),
    LanguageSpec("java", "Java", (".java",), "limited", "text"),
    LanguageSpec("javascript", "JavaScript", (".js", ".jsx"), "limited", "text"),
    LanguageSpec("typescript", "TypeScript", (".ts", ".tsx"), "limited", "text"),
    LanguageSpec("go", "Go", (".go",), "unsupported"),
    LanguageSpec("rust", "Rust", (".rs",), "unsupported"),
    LanguageSpec("csharp", "C#", (".cs",), "unsupported"),
    LanguageSpec("kotlin", "Kotlin", (".kt", ".kts"), "unsupported"),
    LanguageSpec("swift", "Swift", (".swift",), "unsupported"),
    LanguageSpec("php", "PHP", (".php",), "unsupported"),
    LanguageSpec("ruby", "Ruby", (".rb",), "unsupported"),
    LanguageSpec("dart", "Dart", (".dart",), "unsupported"),
)

LANGUAGES_BY_ID = {spec.id: spec for spec in LANGUAGE_SPECS}
LANGUAGE_BY_SUFFIX = {
    extension: spec.id
    for spec in LANGUAGE_SPECS
    for extension in spec.extensions
}


def language_for_path(path: Path) -> str | None:
    return LANGUAGE_BY_SUFFIX.get(path.suffix.lower())


def language_spec(language: str) -> LanguageSpec | None:
    return LANGUAGES_BY_ID.get(language)


def analyzable_extensions() -> list[str]:
    return sorted(
        extension
        for spec in LANGUAGE_SPECS
        if spec.analyzable
        for extension in spec.extensions
    )


def known_extensions() -> set[str]:
    return set(LANGUAGE_BY_SUFFIX)
