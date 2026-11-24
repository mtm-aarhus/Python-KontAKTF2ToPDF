"""KontAKT: hent ét F2-dokument, lav det om til PDF og læg det i KontAKTs filstore.

Queue-driven, ét køelement pr. dokument. For ét F2-dokument:
  1. slår dokumentet op på sit id,
  2. henter ORIGINALEN (``rel/content``),
  3. laver den om til PDF med ``oomtm.convert`` - den samme vej som alle
     KontAKTs PDF-robotter (WeasyPrint til mails, HTML, tekst og CSV,
     LibreOffice til Office, Pillow til billeder),
  4. POSTer bytes ind i KontAKTs filstore (POST .../store).

Kan filen ikke laves om (video, lyd, ukendte filtyper, en beskadiget fil),
lægges originalen op i stedet (``uploaded_original``) med oomtm's grund som
note: den bliver stadig udleveret, den kan bare ikke screenes.

HVORFOR IKKE F2'S EGEN KONVERTERING (rel/pdf-content)
    Den virkede (målt: en 52-byte text/plain kom tilbage som en rigtig PDF), og
    indtil 2026-11 brugte robotten den. Men F2, GO, Nova og mappen gav hver deres
    PDF af det samme dokument - forskellige skrifter, sider og fejl. Nu laves
    alle PDF'er af oomtm, og F2 bruges kun til at HENTE originalen. Samme fil
    giver samme PDF, uanset hvor den kom fra.

    Er dokumentet ALLEREDE en PDF, lader oomtm den være, som den er: det er
    netop den fil, overstregningen senere arbejder på.

En mails vedhæftede filer er deres egne dokumenter i F2, så en mails PDF er
bare mailen.

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
import tempfile
from pathlib import Path
from urllib.parse import quote

import requests

from robot_framework import reset
from robot_framework.exceptions import CaseDeleted
from oomtm import convert
from oomtm import f2 as oomtm_f2
from oomtm import sharepoint as sp  # filename helpers only (build_filename, sanitize_title)

# Har F2-dokumentet ingen FileType, giver ContentType filtypen - oomtm vælger
# motor efter den.
ENDELSE_FRA_CONTENTTYPE = {
    "application/pdf": "pdf",
    "message/rfc822": "eml",
    "application/vnd.ms-outlook": "msg",
    "text/plain": "txt",
    "text/html": "html",
}

ORIGINAL_NOTE = "Filen udleveres, som den er, og bliver ikke screenet."


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

    if not endelse:
        endelse = ENDELSE_FRA_CONTENTTYPE.get(ctype.split(";")[0].strip(), "")

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        # Et fast, kort navn: titlen kan vaere lang nok til at ramme MAX_PATH.
        src = work / f"original.{endelse or 'bin'}"
        src.write_bytes(f2.content(dok))
        hoved = _mailhoved(oc, f2, dok, dok_id, endelse)
        if hoved is not None:
            r = convert.convert_mail(_som_mail(hoved, src, endelse), work / "pdf",
                                     log=oc.log_info)
        else:
            r = convert.convert(src, work / "pdf", log=oc.log_info)
        if r.ok:
            upload, upload_endelse, status = r.pdf, "pdf", "ready"
            # Advarslerne (sporede aendringer, skjulte ark) foelger med PDF'en.
            note = " ".join(r.warnings)
        else:
            oc.log_info(f"oomtm kunne ikke lave .{endelse} om til PDF: {r.note}")
            upload, upload_endelse, status = src, endelse or "bin", "uploaded_original"
            note = f"{r.note} {ORIGINAL_NOTE}"

        akt = akt_id if akt_id is not None else 0
        filnavn = sp.build_filename(akt, dok_id, sp.sanitize_title(title), upload_endelse)
        _gem(client, case_id, doc_id, upload.read_bytes(), filnavn,
             "pdf" if status == "ready" else "original", note)
    return status


# ----- en mail i F2: teksten paa dokumentet, hovedet paa akten ----------------
#
# 2026-11-24: "when you convert mails to pdf make sure you add to/from/cc/subject
# to the top of a mail, regardless if it's a mail received in kontakt, or a mail
# fetched from f2/go/nova case."
#
# En .eml/.msg baerer sit eget hoved, og oomtm saetter det paa (Fra, Sendt, Til,
# Cc, Emne) - det gaelder mails fra GO, Nova og mapper. Men en mail i F2 er en
# AKT: mailens tekst er aktens dokument (HTML), og afsender, modtagere, emne og
# dato staar paa akten. PDF'en af dokumentet alene var bare teksten, uden at man
# kunne se, hvem der skrev til hvem. Saa hentes akten, og hovedet saettes paa.

# Filtyper, der er mailens TEKST og ikke et dokument for sig. En PDF eller et
# Word-dokument paa en indgaaende akt er et brev, og det roeres ikke; en .msg/.eml
# har sit eget hoved.
_TEKST_TYPER = {"html", "htm", "txt"}


def _mailhoved(oc, f2, dok, dok_id: str, endelse: str):
    """Aktens mailhoved (oomtm.f2.mail_fields), hvis dokumentet er en mails
    tekst - ellers None, og dokumentet laves om som foer."""
    if endelse not in _TEKST_TYPER:
        return None
    if not (hasattr(oomtm_f2, "mail_fields") and hasattr(convert, "convert_mail")):
        # oomtm er fra foer 2026-11-24. Saa laves mailen om som foer, uden hoved.
        oc.log_info("oomtm er for gammel til at sætte mailhovedet på en F2-mail. "
                    "Opdatér oomtm-versionen i robottens pyproject.toml.")
        return None
    op = oomtm_f2.links(dok).get("up", "")
    if not op:
        return None
    try:
        akt = f2.get(op)
    except oomtm_f2.F2Error as exc:
        oc.log_info(f"F2-dokument {dok_id}: akten kunne ikke hentes ({exc.status}) "
                    f"- laves om uden mailhoved.")
        return None
    # Kun aktens EGET dokument. Et bilag paa en mail-akt (en .txt, nogen har
    # vedhaeftet) er ikke mailen og skal ikke have dens hoved.
    if oomtm_f2.main_document_id(akt) != str(dok_id):
        return None
    return oomtm_f2.mail_fields(akt)


def _som_mail(hoved: dict, src: Path, endelse: str):
    """En oomtm.mail.Mail af aktens hoved og dokumentets tekst."""
    from oomtm import mail as oomtm_mail
    data = src.read_bytes()
    html = oomtm_mail.decode_html_bytes(data) if endelse in ("html", "htm") else None
    tekst = None if html is not None else oomtm_mail.decode_text_bytes(data)
    return oomtm_mail.Mail(
        subject=hoved.get("subject") or "", sender=hoved.get("sender") or "",
        to=hoved.get("to") or "", cc=hoved.get("cc") or "",
        date=_f2_tid(hoved.get("date")), date_label=hoved.get("date_label") or "Sendt",
        html=html, text=tekst)


def _f2_tid(vaerdi):
    """F2's "2026-09-07T15:24:55.773" (dansk tid, ingen zone) -> datetime.

    Uden zone vises den, som den er - det ER dansk tid. Med zone laver oomtm den
    om til dansk tid."""
    from datetime import datetime
    s = str(vaerdi or "").strip().replace("Z", "+00:00")
    if not s:
        return None
    if "." in s[19:26]:
        # Broekdele af sekunder: Python 3.10 tager kun 3 eller 6 cifre.
        hoved, rest = s[:19], s[19:]
        zone = ""
        for tegn in ("+", "-"):
            if tegn in rest:
                zone = rest[rest.index(tegn):]
        s = hoved + zone
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


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
