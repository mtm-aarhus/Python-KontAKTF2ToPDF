"""KontAKT: hent ét F2-dokument som PDF og læg det i KontAKTs filstore.

Queue-driven, ét køelement pr. dokument. For ét F2-dokument:
  1. slår dokumentet op på sit id,
  2. henter det som PDF - **F2 konverterer selv**,
  3. POSTer bytes ind i KontAKTs filstore (POST .../store).

Kan F2 ikke konvertere filtypen, lægges originalen op i stedet
(``uploaded_original``): den bliver stadig udleveret, den kan bare ikke
OCR-screenes.

HELE KONVERTERINGSKÆDEN ER VÆK, OG DET ER F2'S FORTJENESTE
    GO-udgaven havde tre veje til en PDF: GO's egen konverter først, ellers
    LibreOffice, ellers Pillow eller en mail-render gennem ``oomtm.pdf`` - plus
    ``.goref``-pegepinde, der skulle følges til det rigtige dokument, og en
    ``classify``-tabel over hvad der overhovedet kunne konverteres.

    F2 svarer på det med én relation. Hvert dokument bærer ``rel/pdf-content``,
    og målt: en 52-byte ``text/plain`` kom tilbage som en rigtig PDF på 7966
    byte. Så robotmaskinen behøver ikke LibreOffice installeret, der er ingen
    fallback-kæde at holde ved lige, og resultatet er det samme uanset hvilken
    robotmaskine der spørger - hvilket en lokal LibreOffice aldrig kunne love.

    Er dokumentet ALLEREDE en PDF, hentes ``rel/content`` i stedet. Det er ikke
    en optimering: en PDF, der sendes gennem en konverter igen, kommer ikke
    nødvendigvis ud som den gik ind, og det er netop den fil, overstregningen
    senere arbejder på.

Queue payload (sat af KontAKTs "Hent filer"-udløser):
    {
        "kontakt_case_id": 11,
        "doc_id": 42,                       # case_documents.id
        "source_case_id": "2026 - 559",
        "dok_id": "253533",                 # dokumentets id i F2
        "akt_id": 1,
        "title": "Klage over byggetilladelse",
        "case_title": "Aktindsigt i byggesag"
    }

OO config:
    Constant   F2Miljoe        "test" eller "prod"
    Constant   F2RestTestURL   F2's vaert (med eller uden https://)
    Constant   F2RestProdURL   ditto
    Credential F2TESTRestAkt   F2REST-klientens id + hemmelighed
    Credential F2PRODRestAkt   ditto
    Credential KontAKTAPI      username = base URL, password = X-API-Key
"""
from OpenOrchestrator.orchestrator_connection.connection import OrchestratorConnection
from OpenOrchestrator.database.queues import QueueElement
import json
from urllib.parse import quote

import requests

from robot_framework import reset
from robot_framework.exceptions import CaseDeleted
from oomtm import f2 as oomtm_f2
from oomtm import sharepoint as sp  # filename helpers only (build_filename, sanitize_title)

# Filtyper, der ikke skal sendes til konvertering. Ikke fordi F2 ville svare
# forkert - men en video har ingen PDF-form, og at spoerge er et spildt kald paa
# noget, vi ved svaret paa.
UDEN_PDF_FORM = {
    "mp4", "mov", "avi", "mkv", "wmv", "m4v", "webm", "flv", "mpg", "mpeg",
    "mp3", "wav", "m4a", "aac", "flac", "ogg", "wma",
    "zip", "rar", "7z", "tar", "gz", "iso", "exe", "msi", "dll",
}


# ----- Deleted in KontAKT ----------------------------------------------------


def _check_gone(resp) -> None:
    """Stop cleanly if what this queue element is about was deleted in KontAKT.

    KontAKT answers HTTP 410 with ``{"deleted": "case"|"reference"|"document"}``
    when the caseworker deleted the KontAKT case, the sag/mappe or the document
    while this element waited in the queue. Not an error and not retryable, so
    the queue framework marks the element done and takes the next one.
    """
    if resp is None or resp.status_code != 410:
        return
    try:
        body = resp.json() or {}
    except ValueError:
        body = {}
    if body.get("deleted"):
        raise CaseDeleted(body.get("note") or f"{body['deleted']} deleted in KontAKT")


def process(
    orchestrator_connection: OrchestratorConnection,
    queue_element: QueueElement | None = None,
    client: "reset.Client | None" = None,
) -> None:
    orchestrator_connection.log_trace("Running process.")
    if queue_element is None:
        raise RuntimeError("KontAKTF2ToPDF is queue-driven; no queue_element given.")
    if client is None:  # e.g. a manual run outside the queue framework
        client = reset.open_all(orchestrator_connection)

    payload = json.loads(queue_element.data or "{}")
    case_id = int(payload["kontakt_case_id"])
    doc_id = int(payload["doc_id"])
    dok_id = str(payload["dok_id"]).strip()
    akt_id = payload.get("akt_id")
    title = str(payload.get("title") or "").strip()

    orchestrator_connection.log_info(f"F2ToPDF case={case_id} doc={doc_id} dok={dok_id}")
    _callback(orchestrator_connection, client, case_id, doc_id, {"status": "converting"})

    try:
        status = _hent_og_gem(orchestrator_connection, client, case_id, doc_id,
                              dok_id, akt_id, title)
    except Exception as exc:
        orchestrator_connection.log_info(f"F2ToPDF failed: {exc!r}")
        _callback(orchestrator_connection, client, case_id, doc_id,
                  {"status": "error", "note": str(exc)[:500]})
        raise

    # On success the /store endpoint already recorded status + metadata; only an
    # error needs reporting back via the /file status callback.
    orchestrator_connection.log_info(f"F2ToPDF done doc={doc_id}: {status}")


def _hent_og_gem(oc, client, case_id, doc_id, dok_id, akt_id, title) -> str:
    """Hent dokumentet og laeg det i KontAKTs filstore. Returnerer statussen."""
    f2 = client.f2
    url = f2.rel("document-by-id").replace("{id}", quote(dok_id))
    dok = f2.get(url)
    endelse = (oomtm_f2.text(dok, "FileType") or "").lower().lstrip(".")
    ctype = (oomtm_f2.text(dok, "ContentType") or "").lower()
    oc.log_info(f"F2-dokument {dok_id}: {oomtm_f2.text(dok, 'Title')!r} "
                f".{endelse} {ctype} {oomtm_f2.text(dok, 'Size')} byte")

    data, upload_endelse, status, note = _bytes_til_upload(oc, f2, dok, endelse, ctype)

    akt = akt_id if akt_id is not None else 0
    filnavn = sp.build_filename(akt, dok_id, sp.sanitize_title(title), upload_endelse)
    _gem(client, case_id, doc_id, data, filnavn,
         "pdf" if status == "ready" else "original", note)
    return status


def _bytes_til_upload(oc, f2, dok, endelse, ctype):
    """(bytes, endelse, status, note) for ét F2-dokument.

    status:
      * "ready"             - bytes er en PDF
      * "uploaded_original" - kunne ikke konverteres; originalen lægges op som
                              den er (bliver stadig udleveret, blot ikke
                              OCR-screenet)
    """
    # Allerede en PDF: hent den som den er. Ikke gennem konverteren - det er
    # netop den fil, overstregningen senere arbejder paa.
    if endelse == "pdf" or "pdf" in ctype:
        return f2.content(dok), "pdf", "ready", ""

    if endelse in UDEN_PDF_FORM:
        return (f2.content(dok), endelse or "bin", "uploaded_original",
                f"Filtypen .{endelse} har ingen PDF-form - uploadet som original "
                f"(bliver ikke OCR-screenet).")

    try:
        pdf = f2.pdf_content(dok)
    except oomtm_f2.F2Error as exc:
        oc.log_info(f"F2 kunne ikke konvertere .{endelse} til PDF "
                    f"({exc.status}) - lægger originalen op.")
        return (f2.content(dok), endelse or "bin", "uploaded_original",
                f"F2 kunne ikke konvertere .{endelse} til PDF - uploadet som "
                f"original (bliver ikke OCR-screenet).")
    # F2 svarede 200, men et tomt svar er ikke en PDF. Maalt at et rigtigt svar
    # begynder med %PDF-, saa den er der god grund til at tjekke: en tom fil, der
    # bliver gemt som "ready", ville se udleveringsklar ud og vaere ubrugelig.
    if not pdf[:5] == b"%PDF-":
        oc.log_info(f"F2's pdf-content for .{endelse} var ikke en PDF "
                    f"({len(pdf)} byte) - lægger originalen op.")
        return (f2.content(dok), endelse or "bin", "uploaded_original",
                "F2's PDF-konvertering gav ikke en PDF - uploadet som original "
                "(bliver ikke OCR-screenet).")
    return pdf, "pdf", "ready", ""


def _gem(client, case_id, doc_id, data: bytes, filnavn: str, kind: str, note=""):
    """POST bytes ind i KontAKTs filstore. ``/store`` skriver selv navn,
    stoerrelse, hash og status, saa der er ingen metadata-kvittering bagefter.
    ``kind`` er 'pdf' eller 'original'."""
    r = requests.post(
        f"{client.kontakt_base}/api/v1/cases/{case_id}/documents/{doc_id}/store",
        params={"filename": filnavn, "kind": kind, "note": note or ""},
        headers={"X-API-Key": client.kontakt_key,
                 "Content-Type": "application/octet-stream"},
        data=data, timeout=600,
    )
    _check_gone(r)
    r.raise_for_status()


# ----- KontAKT callback ------------------------------------------------------


def _callback(orchestrator_connection, client, case_id: int, doc_id: int, body: dict) -> None:
    try:
        resp = requests.post(
            f"{client.kontakt_base}/api/v1/cases/{case_id}/documents/{doc_id}/file",
            headers={"X-API-Key": client.kontakt_key, "Content-Type": "application/json"},
            json=body, timeout=30,
        )
    except Exception as exc:  # pylint: disable=broad-except
        orchestrator_connection.log_info(f"Callback to KontAKT failed: {exc!r}")
        return
    # Outside the except: a network blip stays harmless, but "deleted in KontAKT"
    # must reach the framework instead of being swallowed as a broad Exception.
    _check_gone(resp)
