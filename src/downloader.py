from __future__ import annotations

import re
import tempfile
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import (
    HISTORICAL_URL_TEMPLATE,
    REQUEST_TIMEOUT,
    SCOP_DOCUMENTS_URL,
    USER_AGENT,
)


def build_session() -> requests.Session:
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=1.2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
    )
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    return session


def fetch_scop_html(session: requests.Session | None = None) -> str:
    session = session or build_session()
    response = session.get(SCOP_DOCUMENTS_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.text


def _all_links(html: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    links: list[tuple[str, str]] = []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(SCOP_DOCUMENTS_URL, anchor["href"].strip())
        text = " ".join(anchor.stripped_strings)
        links.append((text, href))
    return links


def discover_historical_urls(
    years: list[int] | tuple[int, ...],
    html: str | None = None,
) -> dict[int, str]:
    html = html or fetch_scop_html()
    links = _all_links(html)
    output: dict[int, str] = {}

    for year in years:
        marker = f"CL-Registro-precios-DMA-V-CCA-CCE-{year}".lower()
        for _, href in links:
            if marker in unquote(href).lower():
                output[year] = href
                break

        if year not in output:
            output[year] = HISTORICAL_URL_TEMPLATE.format(year=year)

    return output


def discover_latest_candidates(html: str | None = None) -> list[str]:
    html = html or fetch_scop_html()
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[str] = []

    downloadable = re.compile(r"\.(?:zip|xlsx?|csv)(?:\?|$)", re.I)
    latest_words = re.compile(
        r"(ultim|últim|precio.*actual|actual.*precio|ultimo.*precio)",
        re.I,
    )

    for anchor in soup.find_all("a", href=True):
        href = urljoin(SCOP_DOCUMENTS_URL, anchor["href"].strip())
        text = " ".join(anchor.stripped_strings)
        context = " ".join(
            filter(
                None,
                [
                    text,
                    anchor.get("title", ""),
                    anchor.get("aria-label", ""),
                    Path(urlparse(href).path).name,
                ],
            )
        )
        if downloadable.search(href) and latest_words.search(context):
            candidates.append(href)

    return list(dict.fromkeys(candidates))


def _filename_from_response(response: requests.Response, url: str) -> str:
    disposition = response.headers.get("Content-Disposition", "")
    match = re.search(r'filename\*?=(?:UTF-8\'\')?["\']?([^;"\']+)', disposition, re.I)
    if match:
        return unquote(match.group(1)).strip()

    name = Path(urlparse(response.url or url).path).name
    if name:
        return unquote(name)

    content_type = response.headers.get("Content-Type", "").lower()
    if "spreadsheet" in content_type or "excel" in content_type:
        return "source.xlsx"
    if "csv" in content_type:
        return "source.csv"
    if "zip" in content_type:
        return "source.zip"
    return "source.bin"


def download_to_temp(
    url: str,
    session: requests.Session | None = None,
) -> Path:
    session = session or build_session()
    response = session.get(url, stream=True, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "").lower()
    if "text/html" in content_type:
        preview = response.raw.read(2048, decode_content=True)
        raise RuntimeError(
            f"La URL devolvió HTML en lugar de un archivo descargable: {url}. "
            f"Inicio de respuesta: {preview[:200]!r}"
        )

    filename = _filename_from_response(response, url)
    temp_dir = Path(tempfile.mkdtemp(prefix="fuel_osinergmin_"))
    path = temp_dir / filename

    with path.open("wb") as file:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                file.write(chunk)

    if path.stat().st_size == 0:
        raise RuntimeError(f"OSINERGMIN devolvió un archivo vacío: {url}")

    return path
