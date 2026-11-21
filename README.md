# Python-KontAKTF2ToPDF

Henter **ét F2-dokument**, laver det om til PDF med `oomtm.convert` og lægger
det i KontAKTs filstore, så det kan screenes, overstreges og udleveres.

Køstyret, ét element pr. dokument. Sat i kø af KontAKTs "Overfør filer" og
"Genhent valgte filer".

> Robotten hed `Python-KontAKTGOToPDF`. **Køen heder nu `KontAKTF2ToPDF`** - se
> "Udrulning" nederst.

## F2 henter, oomtm laver PDF'en

1. Dokumentet slås op på sit id.
2. **Originalen** hentes (`rel/content`).
3. Den laves om til PDF med [`oomtm.convert`](https://github.com/mtm-aarhus/oomtm),
   den samme vej som alle KontAKTs PDF-robotter: WeasyPrint til mails, HTML,
   tekst og CSV, LibreOffice til Office, Pillow til billeder. En PDF lades være,
   som den er: det er netop den fil, overstregningen senere arbejder på.
4. Bytes lægges i KontAKTs filstore (`/store`).

Filtypen kommer fra F2's `FileType`, og fra `ContentType`, hvis `FileType` er
tom (fx `message/rfc822` → `.eml`).

## Hvorfor ikke F2's egen konvertering

Indtil 2026-11 hentede robotten F2's PDF (`rel/pdf-content`). Den virkede (målt:
en 52-byte `text/plain` kom tilbage som en rigtig PDF på 7966 byte), men F2, GO,
Nova og mappen gav hver deres PDF af det samme dokument: forskellige skrifter,
sider og fejl. Nu laves alle PDF'er af oomtm, og F2 bruges kun til at hente
originalen. Samme fil giver samme PDF, uanset hvor den kom fra.

Prisen er, at robotmaskinen skal have oomtm's motorer. `reset.open_all` sørger
for det ved første kørsel, uden admin og én gang pr. maskine: LibreOffice
(~350 MB) og WeasyPrints biblioteker (~30 MB), begge fra
[oomtm_setup](https://github.com/mtm-aarhus/oomtm_setup) først.

## Når filen ikke kan blive en PDF

Så lægges **originalen** op i stedet (`uploaded_original`) med oomtm's grund som
note: den bliver stadig udleveret, den kan bare ikke screenes. Det gælder video,
lyd, arkiver, ukendte filtyper og beskadigede filer.

**Ser I en filtype fra F2, som burde kunne blive en PDF, men bliver original**,
så skal den tilføjes i `oomtm.convert` (og i KontAKTs `app/mappe_pdf.py`).
F2's egen konvertering er bevidst ikke en reserve: så ville den samme filtype
give forskellige PDF'er.

En mails vedhæftede filer er deres egne dokumenter i F2, så en mails PDF er bare
mailen.

## Input

| Felt | Betydning |
|---|---|
| `kontakt_case_id` | KontAKT-sagen |
| `doc_id` | `case_documents.id` - rækken, filen hører til |
| `dok_id` | dokumentets id i F2, fx `253533` |
| `akt_id` | aktnummeret; første felt i filnavnet |
| `title` | sagsbehandlerens titel; bliver filnavnets tredje felt |
| `source_case_id`, `case_title` | kun til loggen |

## Output

- `POST /api/v1/cases/{id}/documents/{doc_id}/store` med bytes.
  `/store` skriver selv navn, størrelse, hash og status.
- `POST …/documents/{doc_id}/file` med `{"status": "converting"}` når den
  begynder, og `{"status": "error", "note": …}` hvis det gik galt. Ved succes er
  der ingen kvittering - `/store` har allerede sat statussen.

## Konfiguration

| | |
|---|---|
| Constant `F2Miljoe` | `test` eller `prod` |
| Constant `F2RestTestURL` / `F2RestProdURL` | F2's vært. `https://` må gerne stå der - klienten sætter det kun på, hvis det mangler |
| Credential `F2TESTRestAkt` / `F2PRODRestAkt` | F2REST-klientens id + hemmelighed |
| Credential `KontAKTAPI` | username = base URL, password = X-API-Key |

## Afhængigheder

- `oomtm.f2` - en ordret kopi af `app/integrations/f2_rest.py` i KontAKT-repoet.
  Ret der, og kopiér ud igen.
- `oomtm.sharepoint` bruges **kun** til to rene strengfunktioner,
  `build_filename` og `sanitize_title`, så filnavnene bliver de samme som før.
  SharePoint er ude af KontAKT; det er navnekonventionen, der er tilbage.
- `oomtm.convert` (med `[pdf]`) laver PDF'erne.
- `Pillow` bliver: `robot_framework/error_screenshot.py` importerer PIL, så den
  er en kerneafhængighed for hver robot.

## Udrulning

Mappen er omdøbt, og **`QUEUE_NAME` er skiftet fra `KontAKTGOToPDF` til
`KontAKTF2ToPDF`.** Det er ikke kun en kodeændring:

1. git-remoten i `.git/config` peger stadig på det gamle repositorienavn.
2. OO-processen og dens udløser skal pege på den nye kø.
3. Køelementer i den gamle kø bliver ikke læst af nogen.
