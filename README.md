# Python-KontAKTF2ToPDF

Henter **ét F2-dokument som PDF** og lægger det i KontAKTs filstore, så det kan
screenes, overstreges og udleveres.

Køstyret, ét element pr. dokument. Sat i kø af KontAKTs "Overfør filer" og
"Genhent valgte filer".

> Robotten hed `Python-KontAKTGOToPDF`. **Køen heder nu `KontAKTF2ToPDF`** - se
> "Udrulning" nederst.

## Hele konverteringskæden er væk

GO-udgaven havde tre veje til en PDF: GO's egen konverter først (autoritativ,
klarede mails og Office), ellers LibreOffice headless, ellers Pillow eller en
mail-render gennem `oomtm.pdf`. Plus `.goref`-pegepinde, der skulle følges til
det rigtige dokument, og en `classify`-tabel over hvad der overhovedet kunne
konverteres.

F2 svarer på det med én link-relation. Hvert dokument bærer `rel/pdf-content`,
og det er målt: en 52-byte `text/plain` kom tilbage som en rigtig PDF på 7966
byte (`pdf-content-flattened` gav 8526).

Det betyder tre ting:

1. **Robotmaskinen behøver ikke LibreOffice.** Ingen 350 MB at hente første
   gang, ingen MSI-udpakning uden administratorrettigheder, ingen
   `LIBREOFFICE_PATH` at sætte. `oomtm[pdf]` er ikke længere en afhængighed.
2. **Ingen fallback-kæde at holde ved lige.**
3. **Resultatet er det samme, uanset hvilken robotmaskine der spørger** - hvilket
   en lokalt installeret LibreOffice aldrig kunne love.

## Er dokumentet allerede en PDF, hentes den som den er

`rel/content` og ikke `rel/pdf-content`. Det er ikke en optimering: en PDF, der
sendes gennem en konverter igen, kommer ikke nødvendigvis ud som den gik ind, og
det er netop den fil, overstregningen senere arbejder på.

## Når F2 ikke kan konvertere

Så lægges **originalen** op i stedet (`uploaded_original`): den bliver stadig
udleveret, den kan bare ikke OCR-screenes. Det gælder tre tilfælde:

- **Filtyper uden PDF-form** - video, lyd, arkiver, binære filer. De sendes ikke
  til konvertering: en video har ingen PDF-form, og at spørge er et spildt kald
  på noget, vi kender svaret på.
- **F2 afviser** (`rel/pdf-content` svarer med en fejl).
- **F2 svarer 200 med noget, der ikke er en PDF.** Robotten tjekker for `%PDF-`.
  Uden det tjek ville en tom fil blive gemt som `ready`, se udleveringsklar ud og
  være ubrugelig - og det ville først blive opdaget af ansøgeren.

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
- `Pillow` bliver: `robot_framework/error_screenshot.py` importerer PIL, så den
  er en kerneafhængighed for hver robot.

## Udrulning

Mappen er omdøbt, og **`QUEUE_NAME` er skiftet fra `KontAKTGOToPDF` til
`KontAKTF2ToPDF`.** Det er ikke kun en kodeændring:

1. git-remoten i `.git/config` peger stadig på det gamle repositorienavn.
2. OO-processen og dens udløser skal pege på den nye kø.
3. Køelementer i den gamle kø bliver ikke læst af nogen.

## Uafklaret

`rel/pdf-content-flattened` findes også, og en fladet PDF er præcis det, der bør
udleveres efter en overstregning - hvis "flattened" betyder, at annotationer og
lag er brændt ind. Det er ikke bekræftet, og robotten bruger derfor den
almindelige `pdf-content`. Se `MDFiles/F2-SPOERGSMAAL-TIL-CBRAIN.md`, del 2, punkt 5.
