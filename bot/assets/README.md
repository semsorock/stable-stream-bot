# Post banners

Six reusable 1200 × 400 RGB PNGs in `banners/`, one mint and one burn image
per token. Layout follows the supplied design reference: dark background,
large ticker and authentic token logo, token-colored left stripe, and a
vertical MINTED/BURNED panel. Burn panels are red for every token.

Assets are bundled; posting requires no image generation, downloads, or extra
production dependency. To regenerate with the bundled font and logos:

```sh
uv run --with pillow python scripts/render_banners.py --preview /tmp/stable-stream-banners.png
```

The renderer uses Pillow with 3× supersampling. A fixed, bundled Liberation Sans
Bold font avoids platform font substitutions. Its SIL Open Font License is in
`fonts/LICENSE_LIBERATION`; the unmodified font comes from Mozilla's
[PDF.js standard fonts](https://github.com/mozilla/pdf.js/tree/master/standard_fonts).

## Logo provenance

Fetched 2026-10-19 from the Cardano Foundation's mainnet token metadata service
and registry. The bundled PNGs are the original `logo.value` bytes; rendering
only resizes them. URLs identify exact asset IDs, avoiding ticker collisions.

| Logo | Metadata source | PNG SHA-256 |
| --- | --- | --- |
| USDM | [Mainnet metadata](https://tokens.cardano.org/metadata/c48cbb3d5e57ed56e276bc45f99ab39abe94e6cd7ac39fb402da47ad0014df105553444d) | `5a5f6ef81aa87270fe10d826ef1e48335ead24e54093170f259fc41f5a8bd7fd` |
| USDCx | [Registry entry](https://github.com/cardano-foundation/cardano-token-registry/blob/master/mappings/1f3aec8bfe7ea4fe14c5f121e2a92e301afe414147860d557cac7e345553444378.json) | `8bc5be50c59bc5abe448b7f7ebf1f9f88b2a08119077a1d41aeb897370c3e654` |
| USDA | [Registry entry](https://github.com/cardano-foundation/cardano-token-registry/blob/master/mappings/fe7c786ab321f41c654ef6c1af7b3250a613c24e4213e0425a7ae45655534441.json) | `2e5d868a432f991f0d3e6554d31f4612e2836387b87e2598209acda1778d5af6` |

Logo marks remain their respective owners' property. Registry availability does
not confer trademark ownership or imply endorsement. Use is solely to identify
the asset in informational mint/burn posts; the font license does not cover
the logos. See the registry's
[terms](https://github.com/cardano-foundation/cardano-token-registry/blob/master/Registry_Terms_of_Use.md).
