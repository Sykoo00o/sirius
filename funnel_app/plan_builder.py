"""Deterministic plan builder: quiz answers in, a real 4-week structure out.

This is the engine behind the pre-paywall artefact. It is deliberately *not* an LLM
call. Three reasons, in order of weight:

1. Product truth. The pre-paywall artefact is the thing that must survive a
   deception review, so it must be reproducible from the inputs. A generator we
   cannot re-run identically cannot be defended.
2. Copy discipline. Every string here is written by a human and scanned by
   `claims.violations`, so no model can drift a routine description into a body
   claim at 3am on a paid tier.
3. Cost and latency. Zero inference cost, no timeout budget, and it never returns
   503 on the one screen where the user has just spent five minutes of effort.

The LLM (`llm_flavour.py`) decorates this output with a coach note and a couple of
personalised strings, always with this builder as the fallback. Structure comes from
here; voice comes from there. If the model is dark, the artefact is still real.

Output contract: a `Plan` dataclass, JSON-serialisable via `Plan.to_dict()`. Week 1
is shown free; weeks 2-4 exist in the object but render locked behind the trial.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Vocabulary tables
# ---------------------------------------------------------------------------

DAY_NAMES: tuple[str, ...] = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                              "Saturday", "Sunday")

#: Session archetypes, keyed. `blocks` are (label, minutes-share) pairs; minutes are
#: scaled to the user's stated session length, so a 20-minute plan is a genuinely
#: shorter session, not a truncated 60-minute one.
SESSION_TYPES: dict[str, dict[str, Any]] = {
    "strength_upper": {
        "title": "Upper-body strength",
        "blocks": (
            ("Warm-up: shoulder and wrist prep", 0.15),
            ("Push pattern work", 0.25),
            ("Pull pattern work", 0.25),
            ("Core: bracing and carries", 0.20),
            ("Cooldown: slow breathing, stretch", 0.15),
        ),
    },
    "strength_lower": {
        "title": "Lower-body and hinge",
        "blocks": (
            ("Warm-up: hip openers, glute activation", 0.15),
            ("Squat pattern work", 0.25),
            ("Hinge pattern work", 0.25),
            ("Single-leg balance work", 0.20),
            ("Cooldown: hip and calf stretch", 0.15),
        ),
    },
    "strength_full": {
        "title": "Full-body circuit",
        "blocks": (
            ("Warm-up: full-body mobility flow", 0.15),
            ("Round 1: squat, push, pull", 0.30),
            ("Round 2: hinge, carry, core", 0.30),
            ("Finisher: 3 short efforts", 0.10),
            ("Cooldown: breathing down", 0.15),
        ),
    },
    "cardio_easy": {
        "title": "Easy cardio, conversational pace",
        "blocks": (
            ("Easy start, 5 minutes", 0.15),
            ("Steady effort, nose-breathing pace", 0.65),
            ("Wind down", 0.20),
        ),
    },
    "cardio_intervals": {
        "title": "Intervals",
        "blocks": (
            ("Warm-up", 0.20),
            ("6 to 8 short efforts with recovery", 0.55),
            ("Cooldown", 0.25),
        ),
    },
    "mobility": {
        "title": "Mobility and reset",
        "blocks": (
            ("Spine and hips", 0.35),
            ("Shoulders and thoracic", 0.35),
            ("Breath and down-regulation drill", 0.30),
        ),
    },
    "walk": {
        "title": "Brisk walk",
        "blocks": (
            ("Easy start", 0.15),
            ("Brisk, uninterrupted", 0.70),
            ("Slow finish", 0.15),
        ),
    },
    "rest": {
        "title": "Rest day",
        "blocks": (("No scheduled session. Hydrate, eat properly, sleep.", 1.0),),
    },
}

#: Meal templates per diet preference. Assembly instructions, not recipes, and no
#: portion claims, because portion advice drifts toward outcome claims fast.
MEAL_TEMPLATES: dict[str, dict[str, tuple[str, ...]]] = {
    "anything": {
        "breakfast": ("Eggs and toast with fruit", "Overnight oats with yoghurt and berries",
                      "Smoothie, oats and nut butter"),
        "lunch": ("Chicken or chickpea bowl with rice and salad",
                  "Tuna or tofu wrap with veg", "Leftover dinner, on purpose"),
        "dinner": ("Protein, roasted vegetables, potato or rice",
                   "Pasta with mince or lentil sauce and greens",
                   "Stir-fry with eggs or tofu and rice"),
        "snacks": ("Fruit and nuts", "Yoghurt", "Cheese and crackers"),
    },
    "highprotein": {
        "breakfast": ("Greek yoghurt, protein and berries", "Three-egg scramble with rye toast",
                      "Protein oats with peanut butter"),
        "lunch": ("Double chicken or tofu salad with quinoa", "Tinned fish, beans and salad",
                  "Turkey or tempeh wrap, side of yoghurt"),
        "dinner": ("Large protein portion, potato, green vegetables",
                   "Beef, chilli-bean or tofu stir-fry with rice",
                   "Baked salmon or tempeh, roast vegetables"),
        "snacks": ("Cottage cheese and fruit", "Boiled eggs", "Protein shake with milk"),
    },
    "plant": {
        "breakfast": ("Tofu scramble with sourdough and tomatoes",
                      "Overnight oats with chia, maple and berries",
                      "Smoothie with pea protein, banana and oat milk"),
        # Plant diets default to plant milks and yoghurts, so the dairy restriction must be
        # able to satisfy itself *inside* this template. Without dairy-free primaries here,
        # the generic swap substitutes a soy product into a meal chosen for someone who
        # excluded soy, and the artefact recommends the thing the user just ruled out.
        "_dairy_free_breakfast": ("Overnight oats with coconut yoghurt and seeds",
                                  "Tofu scramble with gluten-free toast",
                                  "Smoothie with oat milk, banana and seeds"),
        "_soy_free_breakfast": ("Chickpea scramble with sourdough",
                                "Overnight oats with chia, maple and berries",
                                "Smoothie with banana, oat milk and seeds"),
        "lunch": ("Lentil and grain bowl with tahini drizzle", "Chickpea curry with rice",
                  "Tempeh sandwich with slaw"),
        "dinner": ("Bean chilli with rice and avocado", "Tofu stir-fry with noodles and greens",
                   "Red lentil pasta with tomato and white beans"),
        "snacks": ("Hummus and carrots", "Trail mix", "Edamame with sea salt"),
        "_dairy_free_snacks": ("Hummus and carrots", "Trail mix",
                               "Coconut yoghurt with toasted oats"),
        "_soy_free_snacks": ("Hummus and carrots", "Trail mix", "Green peas with lemon"),
    },
    "vegetarian": {
        "breakfast": ("Eggs on toast with tomatoes", "Yoghurt, granola and fruit",
                      "Pancakes with nut butter"),
        "lunch": ("Halloumi or egg grain bowl", "Lentil soup with bread and cheese",
                  "Mozzarella, bean and pepper salad"),
        "dinner": ("Paneer, tofu or bean curry with rice", "Mushroom barley risotto with an egg",
                   "Veggie bolognese with pasta"),
        "snacks": ("Apple and peanut butter", "Cheese and olives", "Edamame"),
    },
    "mediterranean": {
        "breakfast": ("Yoghurt, walnuts and honey", "Bread, olive oil, tomato and egg",
                      "Feta and olive omelette"),
        "lunch": ("Grain salad with chickpeas, feta and herbs", "Sardines, beans and lemon",
                  "Chicken and cucumber salad with olive oil"),
        "dinner": ("Fish or chicken, potatoes, green vegetables",
                   "Lamb, bean or chickpea stew with bulgur", "Roasted vegetable pasta with feta"),
        "snacks": ("Olives and bread", "Figs and almonds", "Yoghurt with honey"),
    },
    "lowcarb": {
        "breakfast": ("Eggs, avocado and greens", "Yoghurt with seeds",
                      "Smoked salmon and cream cheese"),
        "lunch": ("Big salad with tuna, egg and olive oil", "Chicken, avocado and coleslaw",
                  "Leftover dinner, cold"),
        "dinner": ("Steak, salmon or tofu with two vegetables", "Cauliflower rice stir-fry with mince",
                   "Sheet-tray chicken and peppers"),
        "snacks": ("Nuts", "Cheese", "Celery and cream cheese"),
    },
    "pescatarian": {
        "breakfast": ("Eggs and smoked fish", "Yoghurt with seeds and fruit", "Oats with milk"),
        "lunch": ("Tinned fish, beans and salad", "Prawn or tofu rice bowl",
                  "Fish cake with salad"),
        "dinner": ("Baked fish, potatoes, greens", "Prawn or white-bean pasta",
                   "Salmon, sweet potato and broccoli"),
        "snacks": ("Tinned tuna on crackers", "Yoghurt", "Boiled egg"),
    },
}

#: What each exclusion *means* when reading a meal sentence.
#:
#: Two lists, because they answer different questions. `names` decides whether a phrase is
#: allowed at all for this visitor; `remove` says which fragments to cut out if it is not.
#: They differ where a composite food contains an excluded ingredient rather than being it:
#: "peanut butter" violates nuts, but cutting the matched word "nuts" out of it would leave
#: "pea butter".
#:
#: One shared `ALLOWED_QUALIFIED` list sits below the table rather than one per exclusion.
#: "milk" and "yoghurt" are exactly the words the plan itself writes *because* someone
#: excluded dairy ("oat milk", "coconut yoghurt"), so the qualification belongs to the word
#: rather than to the exclusion, and one list is one fewer place to forget an update.
#:
#: This table also drives `_audit_meal_tables`, which re-checks every template below against
#: it. That is what keeps the vocabulary honest as the tables are edited, because the audit
#: reads the data, not the algorithm.
RESTRICTION_VOCAB: dict[str, dict[str, tuple[str, ...]]] = {
    "dairy": {
        # "seed butter", "sunflower butter" and friends are spreads, not dairy, and the nuts
        # rule offers them as the answer. `ALLOWED_QUALIFIED` masks the specific spellings the
        # tables use; this is where that masking is *needed*, so a new "... butter" spread
        # added later is a missing mask entry rather than a dairy recommendation.
        "names": ("yoghurt", "yogurt", "cheese", "milk", "butter", "cream", "feta",
                  "halloumi", "haloumi", "paneer", "mozzarella", "whey", "casein",
                  "kefir", "ghee"),
        "remove": ("yoghurt", "yogurt", "milk", "butter", "cream", "cheese", "feta",
                   "halloumi", "haloumi", "paneer", "mozzarella", "whey")
    },
    "lactose": {
        # Lactose is the sugar, not the food. Hard cheese and butter carry very little of
        # it, so flagging "butter" here would over-restrict and the plan would read as if
        # it had not listened to a "lactose" tick versus a "dairy" tick.
        "names": ("milk", "ice cream", "pancake", "crepe", "mousse", "pudding",
                  "buttermilk", "yoghurt", "yogurt"),
        "remove": ("milk", "yoghurt", "yogurt")
    },
    "soy": {
        "names": ("soy", "soya", "tofu", "tempeh", "edamame", "miso", "whey", "soybean"),
        "remove": ("soy", "soya", "tofu", "tempeh", "edamame", "miso", "whey")
    },
    "gluten": {
        "names": ("wheat", "barley", "rye", "sourdough", "pasta", "bread", "wrap", "granola",
                  "noodle", "couscous", "bulgur", "spelt", "semolina", "bolognese", "toast",
                  "cracker", "breadcrumb"),
        "remove": ("wheat", "barley", "rye", "sourdough", "pasta", "bread", "wrap", "granola",
                   "noodle", "couscous", "bulgur", "spelt", "bolognese", "toast", "cracker"),
    },
    "nuts": {
        "names": ("nut", "peanut", "almond", "cashew", "pistachio", "hazelnut", "pecan",
                  "walnut", "macadamia", "trail mix"),
        # "nut" is matched as a whole word (see `_names_excluded_food`): substring matching
        # would flag "coconut", which most nut allergics do eat, and "peanut butter" is
        # covered by its own entry anyway.
        "remove": ("nuts", "peanut butter", "almonds", "walnuts", "trail mix", "nut butter"),
    },
    "egg": {
        "names": ("egg", "mayonnaise", "aioli", "carbonara"),
        "remove": ("eggs", "egg", "mayonnaise", "aioli"),
    },
    "shellfish": {
        "names": ("prawn", "shrimp", "crab", "lobster", "scallop", "mussel", "clam",
                  "crayfish", "langoustine"),
        "remove": ("prawns", "prawn", "shrimp", "crab", "lobster", "scallops", "mussels"),
    },
    "redmeat": {
        "names": ("beef", "lamb", "venison", "mince", "steak", "bacon", "sausage", "duck",
                  "chorizo", "liver"),
        "remove": ("beef", "lamb", "venison", "steak", "mince", "bacon", "sausage"),
    },
    # Alcohol names nothing in a meal structure. That is an honest result, not a gap: the
    # answer changes the timing advice and the snacks slot, and inventing a dinner rewrite
    # for it would be decoration.
    "alcohol": {"names": (), "remove": ()},
    "coffee": {"names": ("coffee", "espresso", "latte", "flat white", "pre-workout"),
               "remove": ("coffee",)},
}

#: Qualified words that stop being the excluded food. "oat milk" is not dairy and "coconut
#: yoghurt" is not dairy either, which is the whole reason a plant-eater who excludes dairy
#: has any breakfast templates left.
#:
#: Seed spreads are here because the nuts rule answers "peanut butter" with "sunflower seed
#: butter", and dairy matches on "butter". Writing every seed out ("sunflower butter",
#: "pumpkin butter", "peanut butter", ...) turns one missing name into a dairy recommendation
#: on a nut-free plan, so seed-noun phrases are masked by pattern instead.
ALLOWED_QUALIFIED: tuple[str, ...] = (
    "oat milk", "almond milk", "coconut milk", "soy milk", "rice milk", "cashew milk",
    "pea milk", "coconut yoghurt", "coconut yogurt", "soy yoghurt", "oat yoghurt",
    "coconut cream", "cocoa butter", "shea butter", "maca", "soy lecithin",
)

#: Product-family phrases masked by pattern rather than enumerated: seed spreads, and seed or
#: nut flours, which "nuts" would otherwise match on the word "nut".
_ALLOWED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b[a-z]+ (?:seed |nut )?butter\b"),
    re.compile(r"\b(?:seed|nut) flour\b"),
)

#: Compounds that are *not* the dish named inside them, masked before the exclusion patterns
#: run, so "Three-egg scramble" is invisible to `egg` the same way "oat milk" is to `dairy`.
#: The templates name this dish hyphenated in two places; the pattern's `\beggs?\b` matches
#: the segment after the hyphen, so without masking it here, an egg-excluding high-protein
#: visitor gets "Three-tofu scramble", which is the artefact misreading its own handwriting.
COMPOUND_DISHES: tuple[str, ...] = ("three-egg scramble",)

#: Phrases masked when the exclusion being tested is the one named inside them.
#:
#: Distinct from `ALLOWED_QUALIFIED`, which masks unconditionally: "gluten-free toast" *is*
#: gluten, so masking it for everyone would hide real gluten. It is fine only for the
#: visitor who said gluten, for whom "free" is the operative morpheme.
#:
#: Needed because a swap rule that expands a composite drags its whole name with it: the
#: gluten rule rewrites "bolognese" to "gluten-free mince sauce", and a person excluding both
#: gluten and red meat would be handed "mince" inside the sentence written for them. Same
#: class of bug as the dairy rule naming soy, arriving by a different door, which is the
#: argument for masking at the vocabulary level rather than special-casing one table.
FREE_FROM_QUALIFIED: tuple[str, ...] = ("gluten-free", "lactose-free", "dairy-free",
                                        "soy-free", "nut-free", "alcohol-free")

#: Phrases masked before any exclusion is matched, longest first, so a permitted composite
#: can not be hit by the word inside it.
_MASKED_PHRASES: tuple[str, ...] = tuple(sorted(
    (phrase.lower() for phrase in (*ALLOWED_QUALIFIED, *COMPOUND_DISHES)),
    key=len, reverse=True))

#: The exclusion label, which `RESTRICTION_VOCAB` does not store.
#:
#: `names` lists what a meal sentence might say ("wheat", "rye", "sourdough"). The label a
#: shopper reads names the *category* ("gluten"), and that word is in no list, so a matcher
#: built from `names` alone is silently deaf to "gluten-free": the mask matches nothing, the
#: code looks like it is masking something, and the plan keeps serving toast. `alcohol` is
#: the mirror image, with `names` empty and the label doing all the work.
#: Exclusion pairs that describe the same shelf, where a substitute may name the wider
#: family. Lactose intolerance is answered with lactose-free dairy, which is dairy; the
#: visitor who also ticked "dairy" is served by the runtime candidate check, which rejects the
#: value for them. Recording the pair here makes that an exception with a reason, rather than
#: a hole someone punches in a test when the build goes red.
_FAMILY_OVERLAPS: frozenset[str] = frozenset({"dairy", "lactose"})

CATEGORY_LABELS: dict[str, str] = {
    "dairy": "dairy", "lactose": "lactose", "soy": "soy", "gluten": "gluten",
    "nuts": "nut", "egg": "egg", "shellfish": "shellfish", "alcohol": "alcohol",
    "coffee": "coffee",
    # "redmeat" is a category with no food word behind it ("lamb" already trips the matcher
    # on its own), but a label is still needed for the collective spelling: "meat-free mince"
    # has to read as meat-free to the visitor who ticked it.
    "redmeat": "meat",
}

#: Per-exclusion "free from" masks, applied only while testing that exclusion.
#:
#: Regex rather than literal replace, because the phrases in the tables are inflected:
#: "gluten-free toast", "wheat free bread", "lactose-free milk". A literal "gluten-free"
#: lookup finds nothing in "gluten-free toast or potato", which is exactly what the gluten
#: rule emits. Optional morphology on both sides (`s?`, "less", hyphen or space) covers
#: "gluten free", "gluten-free" and "wheat free" alike.
#:
#: A free-from label qualifies the item it sits on, so the match consumes that item: a
#: leading ingredient name, an optional conjunction, then the item proper. Masking the label
#: alone was the bug, and it was invisible for a satisfying reason. Masking "gluten-free"
#: leaves "toast", and "toast" is itself a gluten name word, so the phrase still read as
#: gluten to precisely the visitor it was written for. A label certifies the dish, not one
#: ingredient, which is why this is a phase that removes a span rather than another word in
#: the alternation.
#:
#: Built with an explicit loop: a comprehension here would rebind `restriction`, the name
#: `_names_excluded_food` and several callers iterate, and a table keyed off whichever
#: iteration happened to land last reads as a working feature while behaving like a coin flip.
_FREE_FROM_PATTERNS: dict[str, re.Pattern[str]] = {}
for _exclusion, _rule in RESTRICTION_VOCAB.items():
    # "gluten-free toast or potato" -> the label plus the conjunction plus "toast", one span.
    # `[^,/]` keeps the span inside one dish, so "Coconut yoghurt with granola" under a
    # gluten-free label does not swallow the whole sentence.
    _stems = sorted({*_rule["names"], CATEGORY_LABELS[_exclusion]}, key=len, reverse=True)
    _stem_group = "(?:" + "|".join(re.escape(word) for word in _stems) + ")"
    _item = r"[a-z]+(?: [a-z]+){0,2}"                       # "brown", "rye sourdough"
    _FREE_FROM_PATTERNS[_exclusion] = re.compile(
        # A: "gluten-free toast", "gluten free and toast", "wheat-free brown bread"
        _stem_group + r"s?\s*(?:-\s*)?(?:free|less)(?:\s+(?:and\s+|or\s+)?[_a-z]+){0,2}"
        # B: "toast free of gluten", "bread gluten free" (label after the item)
        + r"|(?:[_a-z]+ ){0,2}" + _stem_group + r"s?\s*(?:-\s*)?(?:free|less)")
del _exclusion, _rule, _stems, _stem_group, _item

#: Word-boundary matchers, compiled once. Plural-tolerant on purpose: "Nuts" and "nuts"
#: are the same food, and a case- or plurality-sensitive check would leak one through.
_WORD_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    restriction: tuple(re.compile(rf"\b{re.escape(word)}s?\b", re.IGNORECASE)
                       for word in rule["names"])
    for restriction, rule in RESTRICTION_VOCAB.items()
}


def _masked(text: str) -> str:
    """Blank out permitted composite phrases, position-preserving.

    Length-preserving on purpose: the offsets stay usable if a caller ever wants the match
    back, and the code does not have to remember that masking shifted the string.
    """
    masked = text.lower()
    # Patterns first: "sunflower seed butter" must be masked as a unit, and doing the literal
    # pass first would leave "seed butter" half-exposed to whatever the literals cover.
    for pattern in _ALLOWED_PATTERNS:
        masked = pattern.sub(lambda m: "_" * len(m.group(0)), masked)
    for phrase in _MASKED_PHRASES:
        masked = masked.replace(phrase, "_" * len(phrase))
    return masked


def _names_excluded_food(text: str, restrictions) -> bool:
    """True when `text` names something this visitor ruled out.

    Whole-word, plural-tolerant, case-insensitive, after masking the permitted composites.
    "Overnight oats with coconut yoghurt" must not trip `nuts`, because coconut is not a
    tree nut, and must trip `dairy`, because yoghurt is the food. Both mistakes show
    themselves to the visitor within a second: a nut-free plan offering almonds reads as
    ignored, and a nut-free plan refusing coconut reads as a robot that never read the box.

    Per exclusion rather than once for the whole set, so `FREE_FROM_QUALIFIED` can mask
    "gluten-free" while checking gluten and still leave "gluten" visible when the question is
    about dairy. Masking globally would let real gluten through to a gluten-eater's plan.
    """
    for restriction in restrictions:
        if restriction == "none":
            continue
        # Conditional masks first. Doing it after `_masked` would be too late: `ALLOWED_QUALIFIED`
        # blanks "oat milk" and friends to underscores, and "lactose-free oat milk" would lose
        # the part the restriction still needs to see.
        lowered = text.lower()
        free_from = _FREE_FROM_PATTERNS.get(restriction)
        if free_from:
            lowered = free_from.sub(lambda m: "_" * len(m.group(0)), lowered)
        if any(pattern.search(_masked(lowered))
               for pattern in _WORD_PATTERNS.get(restriction, ())):
            return True
    return False


#: Restriction substitutions applied after the diet templates are picked. Structural
#: swaps only. This is not allergy advice: the funnel offers a structure the user can
#: edit, and says so.
RESTRICTION_SWAPS: dict[str, tuple[tuple[str, str], ...]] = {
    "gluten": (("sourdough", "rice or cornbread"), ("toast", "gluten-free toast or potato"),
               ("rye", "gluten-free grain"), ("pasta", "rice or corn pasta"),
               ("barley", "rice"), ("bulgur", "quinoa"), ("wrap", "corn or lettuce wrap"),
               ("bread", "gluten-free bread"), ("granola", "toasted oats certified gluten-free"),
               ("noodles", "rice noodles"),
               # "mince sauce" was the old phrasing and it is a trap: mince is red meat, so a
               # visitor excluding both gluten and red meat was handed "mince" inside the
               # sentence written for them. Lentils keep the shape of the dish and offend
               # nobody.
               ("bolognese", "gluten-free lentil sauce")),
    # Replacement text is the artefact: whoever excluded dairy reads these words as the
    # answer, so each one is a food rather than an instruction. "nutritional yeast or omit"
    # tells the visitor to decide, which is a form field, not a plan. Candidates that would
    # collide with another exclusion are filtered out at runtime, so the plant-milk wording
    # here is the general-case answer and the soy-excluder simply gets a different template.
    # Every substitute here is soy-free, and that constraint is the point rather than a
    # coincidence. Dairy and soy substitute for each other so often in this niche (yoghurt for
    # yoghurt, tofu for cheese) that a dairy rule written with tofu answers will always fail a
    # dairy+soy visitor, and the runtime rejection path then has to find a template instead.
    # Choosing coconut, oats, chickpeas and hummus keeps this rule usable for both, which is
    # one fewer silent fallback on the screen being judged.
    "dairy": (("yoghurt", "coconut yoghurt"), ("cottage cheese", "silken coconut yoghurt"),
              ("cream cheese", "hummus"), ("cheese", "nutritional yeast"),
              ("feta", "marinated chickpea crumble"),
              ("halloumi", "grilled chickpea slab"), ("haloumi", "grilled chickpea slab"),
              ("paneer", "chickpea slab"), ("butter", "olive oil"), ("milk", "oat milk"),
              ("mozzarella", "extra tomato and olive oil")),
    "lactose": (("yoghurt", "lactose-free yoghurt"), ("milk", "lactose-free milk"),
                ("ice cream", "sorbet")),
    # Alcohol names nothing in a meal structure, and pretending otherwise would be
    # decoration: the answer lands in the timing advice instead. An empty tuple is that
    # decision written down rather than a missing key.
    "alcohol": (),
    # "nut butter" before "peanut butter" before "nuts": `_swap_rules` sorts longest-first,
    # so "Pancakes with nut butter" swaps as a unit instead of losing the inner "nut" and
    # becoming "Pancakes with nut olive oil". Same mechanism settles "walnuts" against
    # "nuts", which is why "Yoghurt, walnuts and honey" must never fall through to a bare
    # "walnuts" hit.
    "nuts": (("nut butter", "sunflower seed butter"),
             ("peanut butter", "sunflower seed butter"),
             ("walnuts", "pumpkin seeds"), ("almonds", "pumpkin seeds"),
             ("trail mix", "seeds and dried fruit"),
             ("nuts", "pumpkin or sunflower seeds")),
    # Tempeh is soy, so the old "cod or tempeh" answer failed a shellfish+soy visitor. The
    # rule is also scoped to the two phrases that actually occur in the templates rather than
    # to bare "salmon", which appears in "Smoked salmon and cream cheese": a broad rule there
    # would rewrite a dish that never contained shellfish.
    "shellfish": (("prawns", "chicken or cod"), ("prawn", "chicken or cod"),
                  ("shrimp", "cod"), ("salmon or tempeh", "cod or white beans")),
    # No "Three-egg scramble" rule: that phrase is masked out of the exclusion check rather
    # than swapped, because a rule for it would fire on an egg-eater too. `egg` accepts a
    # breakfast that does not contain egg, which is worth stating next to the mask list.
    # Legume-based rather than soy-based, for the same reason the dairy rules avoid tofu, and
    # "chickpea omelette" is a real dish rather than an apology for one.
    "egg": (("boiled eggs", "roasted chickpeas"), ("Boiled egg", "Roasted chickpeas"),
            ("omelette", "chickpea omelette"), ("eggs", "chickpea scramble"),
            ("egg", "chickpea scramble")),
    "soy": (("soy yoghurt", "coconut yoghurt"), ("tofu scramble", "chickpea scramble"),
            ("tempeh", "beans or chicken"), ("edamame", "green peas"),
            ("tofu", "chickpeas"), ("soy", "lemon and olive oil")),
    # "lentil or turkey mince" is a lie about a product most shelves do not carry, and
    # `redmeat` overlaps `mince` in ordinary speech. Lentils are the honest swap.
    "redmeat": (("Beef", "Lentils or turkey"), ("steak", "salmon or chicken thigh"),
                ("mince", "lentil sauce"), ("lamb", "chicken thigh")),
    "coffee": (("Coffee", "Decaf or herbal tea"),),
}

BLOCKER_FALLBACKS: dict[str, str] = {
    "time": ("A 15-minute version of any session is on the card, so a busy day "
             "shrinks the session instead of skipping it."),
    "energy": ("Hard sessions sit on your two best-energy days, and the day after "
               "them is walk-only."),
    "motivation": ("Week 2 changes the session selection, not just the load, so the "
                   "novelty drop has somewhere to go."),
    "travel": ("Every session has a hotel-room variant that needs no equipment."),
    "knowledge": ("Each session lists its blocks in order with set-and-rep ranges, so "
                  "the decision is made before you arrive."),
    "soreness": ("Strength days alternate, and a swap to mobility is pre-approved "
                 "rather than something you have to justify."),
    "food": ("Two of the meal slots reuse the same cooked base, so a bad food day is "
             "an assembly job, not a failure."),
    "work": ("Sessions sit in the time block you said was real, and shift weeks use "
             "the minimum-viable week: two sessions plus walks."),
}

GOAL_FOCI: dict[str, tuple[str, ...]] = {
    "energy": ("Sessions on your two best-energy days, not your theoretically free ones",
               "Carry a bottle and drink through the morning",
               "One walk without headphones, once a week"),
    "strength": ("Add one clean rep or 2.5 kg to the main lift each week",
                 "Log every working set, even the bad ones",
                 "Never skip the warm-up on heavy days"),
    "lean": ("Three meal slots a day, chosen rather than improvised",
             "Protein at breakfast, every day",
             "Steps as the baseline metric, sessions as the bonus"),
    "endurance": ("Keep the easy day genuinely easy: you should be able to talk",
                  "One interval session a week, no more",
                  "Track resting heart rate on waking twice a week"),
    "mobility": ("Ten minutes of movement on the days you do not train",
                 "Work the two tightest areas, not all of them",
                 "Note range changes weekly rather than daily"),
    "sleep": ("Screens off fifteen minutes before your target bedtime",
              "Same wake time seven days a week, weekend included",
              "Caffeine cut-off set from your evening session time"),
    "consistency": ("Never miss twice in a row: one skipped day is noise, two is a trend",
                    "Kit out the night before",
                    "Mark the calendar, keep the streak visible"),
}

#: Progressive structure for weeks 2-4. Locked behind the trial, so the strings only
#: need to be honest about what changes, not detailed enough to be worth stealing.
WEEK_ARCS: tuple[dict[str, str], ...] = (
    {"week": 1, "label": "Baseline week",
     "arc": "Everything is a first pass. We are learning what your body and your "
            "calendar actually do with the plan, not pushing."},
    {"week": 2, "label": "First adjustment",
     "arc": "Session selection changes where week 1 felt wrong, and the main lifts "
            "or intervals step up one notch."},
    {"week": 3, "label": "Volume step",
     "arc": "One extra working round on the two hardest sessions, meal prep moves to "
            "two batch slots."},
    {"week": 4, "label": "Consolidate and review",
     "arc": "Load holds steady, sleep and check-in metrics get reviewed, and the next "
            "cycle is written from the four weeks of data rather than from optimism."},
)


# ---------------------------------------------------------------------------
# Data contract
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Profile:
    """Validated quiz answers reduced to the fields the builder actually uses."""

    goal: str
    experience: str
    days_per_week: int
    session_minutes: int
    time_of_day: str
    weekend_shape: str
    diet: str
    restrictions: frozenset[str]
    equipment: frozenset[str]
    cook_style: str
    sleep: str
    blocker: str
    notes: dict[str, str] = field(default_factory=dict)

    @property
    def primary_equipment(self) -> str:
        for preferred in ("fullgym", "homegym", "dumbbells", "bands", "pool", "classes",
                          "outdoor", "bodyweight"):
            if preferred in self.equipment:
                return preferred
        return "bodyweight"

    @property
    def sleep_flag(self) -> bool:
        """Short or broken sleep: the builder lowers scheduled load, gently."""
        return self.sleep in {"under6", "broken"}

    def signature(self) -> str:
        """Stable short hash of the inputs, shown in the plan footer.

        Pointedly visible: it is the receipt that the same twelve answers produce the
        same plan, which is what makes the artefact inspectable rather than vibes.
        """
        payload = json.dumps(asdict(self), sort_keys=True, default=sorted)
        return hashlib.sha256(payload.encode()).hexdigest()[:8]


@dataclass(frozen=True)
class Session:
    name: str
    focus: str
    minutes: int
    blocks: tuple[tuple[str, int], ...]
    rpe_guide: str
    fallback: str


@dataclass(frozen=True)
class DayPlan:
    day: str
    session: Session
    meal_structure: tuple[tuple[str, str], ...]
    focus_metric: str
    is_training_day: bool


@dataclass(frozen=True)
class Plan:
    headline: str
    summary: str
    days_per_week: int
    session_minutes: int
    split_label: str
    week_arc: dict[str, str]
    days: tuple[DayPlan, ...]
    metrics: tuple[tuple[str, str, str], ...]
    fallback_rule: str
    profile_signature: str
    source: str  # "builder" | "builder+llm"
    #: Opening coach note. The builder writes a truthful default; `llm_flavour` may
    #: replace it with generated voice, and only after the copy guardrail scans it.
    #: Structure never comes from the model, so this is the one field decoration touches.
    coach_note: str = ""

    def with_coach_note(self, note: str, *, source: str | None = None) -> "Plan":
        """Return a copy with a different coach note. Frozen dataclass, so explicit
        replacement rather than a mutable field someone can half-update."""
        return Plan(**{**self.to_dict_flat(), "coach_note": note,
                       "source": source or self.source})

    def to_dict_flat(self) -> dict[str, Any]:
        """Constructor kwargs for an equal `Plan`, for `with_coach_note`."""
        return {
            "headline": self.headline,
            "summary": self.summary,
            "days_per_week": self.days_per_week,
            "session_minutes": self.session_minutes,
            "split_label": self.split_label,
            "week_arc": self.week_arc,
            "days": self.days,
            "metrics": self.metrics,
            "fallback_rule": self.fallback_rule,
            "profile_signature": self.profile_signature,
            "source": self.source,
            "coach_note": self.coach_note,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "headline": self.headline,
            "summary": self.summary,
            "days_per_week": self.days_per_week,
            "session_minutes": self.session_minutes,
            "split_label": self.split_label,
            "week_arc": self.week_arc,
            "days": [
                {
                    "day": d.day,
                    "is_training_day": d.is_training_day,
                    "session": {
                        "name": d.session.name,
                        "focus": d.session.focus,
                        "minutes": d.session.minutes,
                        "blocks": [{"label": label, "minutes": minutes}
                                   for label, minutes in d.session.blocks],
                        "rpe_guide": d.session.rpe_guide,
                        "fallback": d.session.fallback,
                    },
                    "meal_structure": [{"slot": slot, "ideas": ideas}
                                       for slot, ideas in d.meal_structure],
                    "focus_metric": d.focus_metric,
                }
                for d in self.days
            ],
            "metrics": [{"label": label, "target": target, "why": why}
                        for label, target, why in self.metrics],
            "fallback_rule": self.fallback_rule,
            "profile_signature": self.profile_signature,
            "source": self.source,
            "coach_note": self.coach_note,
        }


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

#: Preferred weekday indices per requested day count, chosen so training days spread
#: across the week and heavy sessions never land back to back.
DAY_PATTERNS: dict[int, tuple[int, ...]] = {
    2: (1, 4),
    3: (1, 3, 5),
    4: (1, 2, 4, 5),
    5: (0, 1, 2, 4, 5),
    6: (0, 1, 2, 3, 4, 5),
}

#: Session-type sequences per day count, ordered so hard days alternate.
SEQUENCES: dict[int, tuple[str, ...]] = {
    2: ("strength_full", "strength_full"),
    3: ("strength_upper", "cardio_intervals", "strength_lower"),
    4: ("strength_upper", "strength_lower", "cardio_intervals", "strength_full"),
    5: ("strength_upper", "strength_lower", "cardio_intervals", "strength_full", "cardio_easy"),
    6: ("strength_upper", "cardio_intervals", "strength_lower", "mobility",
        "strength_full", "cardio_easy"),
}

TIME_SLOT_LABELS: dict[str, str] = {
    "early": "early morning, before the day fills up",
    "midday": "your midday break",
    "evening": "early evening, straight after work",
    "late": "late evening, kept short so it does not cost sleep",
    "varies": "whatever slot survives the day, which is why the fallback matters",
}


def _scale(blocks: tuple[tuple[str, float], ...], minutes: int) -> tuple[tuple[str, int], ...]:
    """Turn minute-shares into whole minutes that still total the session length."""
    raw = [(label, share * minutes) for label, share in blocks]
    scaled = [(label, max(2, int(round(value)))) for label, value in raw]
    drift = minutes - sum(value for _, value in scaled)
    if drift:
        # Adjust the longest block, which is where a minute either way is invisible.
        index = max(range(len(scaled)), key=lambda i: scaled[i][1])
        label, value = scaled[index]
        scaled[index] = (label, max(2, value + drift))
    return tuple(scaled)


def _session_type_for(profile: Profile, base: str) -> str:
    """Reduce load where the inputs say recovery is already short."""
    if profile.sleep_flag and base == "cardio_intervals":
        return "cardio_easy"
    if profile.experience == "new" and base == "strength_full":
        return "strength_upper"
    return base


def _rpe_guide(profile: Profile, base: str) -> str:
    if profile.experience == "new":
        return "Stop each set with 3 clean reps left. Technique is the progress metric this month."
    if profile.experience == "comeback":
        return "2 to 3 reps in reserve for the first two weeks, then 1 to 2."
    if profile.experience == "solid":
        return "1 to 2 reps in reserve on top sets, 2 to 3 on accessories."
    return "Hold 2 reps in reserve; the plan steps load before it steps effort."


def _fallback(profile: Profile) -> str:
    return BLOCKER_FALLBACKS.get(profile.blocker, BLOCKER_FALLBACKS["time"])


def _schedule(profile: Profile) -> list[tuple[int, str]]:
    count = max(2, min(6, profile.days_per_week))
    pattern = DAY_PATTERNS[count]
    sequence = SEQUENCES[count]
    if profile.weekend_shape == "shift":
        # Weekend warrior: shift the block so the sessions sit where they said they train.
        pattern = tuple(((index + 4) % 7) for index in pattern)
    elif profile.time_of_day == "early":
        pattern = tuple(min(6, index) for index in pattern)
    return sorted(zip(pattern, sequence, strict=False), key=lambda pair: pair[0])


#: Restriction values that select a "*_free_" template variant for a slot. Lactose rides
#: with dairy because the dairy-free variants are what a lactose-free request resolves to
#: in practice: the plan offers plant yoghurt, which satisfies both readings.
FREE_FROM_GROUPS: dict[str, str] = {
    "dairy": "dairy",
    "lactose": "dairy",
    "soy": "soy",
}


def _free_from_variants(diet_templates: dict[str, tuple[str, ...]],
                        restrictions: frozenset[str]) -> dict[str, tuple[str, ...]]:
    """Collect the `_<group>_free_<slot>` variants this visitor's answers call for."""
    variants: dict[str, tuple[str, ...]] = {}
    wanted = {FREE_FROM_GROUPS[r] for r in restrictions if r in FREE_FROM_GROUPS}
    for slot, ideas in diet_templates.items():
        for group in wanted:
            prefix = f"_{group}_free_"
            if slot.startswith(prefix):
                base = slot[len(prefix):]
                variants[base] = variants.get(base, ()) + ideas
    return variants


def _swap_rules(restrictions: frozenset[str]) -> tuple[tuple[str, str], ...]:
    """Merge every applicable restriction's swaps into one longest-first rule list.

    Deduped by search term, longest search term first. That ordering is what makes the
    specific phrase win inside its own substring: "soy yoghurt" swaps as one unit rather
    than becoming "soy coconut yoghurt" via a lone "yoghurt" rule. Because the ordering does
    the work, the tables above can be edited in any order, which is the property worth
    buying: rule authoring stops requiring knowledge of what other rules contain.
    """
    best: dict[str, tuple[str, str]] = {}
    for restriction in restrictions:
        for old, new in RESTRICTION_SWAPS.get(restriction, ()):
            key = old.lower()
            if key not in best or len(old) > len(best[key][0]):
                best[key] = (old, new)
    return tuple(sorted(best.values(), key=lambda pair: len(pair[0]), reverse=True))


def _apply_swaps(phrase: str, rules: tuple[tuple[str, str], ...]) -> str:
    """One left-to-right scan of the *original* text, rebuilt from untouched spans.

    Two phases rather than chained `str.replace` calls, and the reason is structural:
    a replacement is never re-read. "coconut yoghurt" contains "yoghurt", so any approach
    that rescans its own output swaps it twice and produces "coconut soy or coconut
    yoghurt", naming the excluded food inside the sentence written to avoid it.
    """
    if not rules or not phrase:
        return phrase
    lowered = phrase.lower()
    spans: list[tuple[int, int, str]] = []
    taken_to = 0
    for old, new in rules:                      # longest-first, so specific beats general
        start = lowered.find(old, taken_to)
        if start == -1:
            continue
        spans.append((start, start + len(old), new))
        taken_to = start + len(old)
    if not spans:
        return phrase
    rebuilt: list[str] = []
    cursor = 0
    for start, end, new in spans:               # already ordered by position
        rebuilt.append(phrase[cursor:start])
        # Preserve a sentence-leading capital: "Beef, ..." must not become "lamb, ...".
        rebuilt.append(new[0].upper() + new[1:]
                       if phrase[start].isupper() and new[:1].islower() else new)
        cursor = end
    rebuilt.append(phrase[cursor:])
    return "".join(rebuilt)


def _repair_slot(phrase: str, restrictions: frozenset[str]) -> str | None:
    """Cut the offending fragments out of a phrase, or return None if too little survives.

    Used only when nothing clean exists to substitute. Subtracting is truthful in a way
    substituting is not: "Overnight oats with yoghurt and berries" under dairy becomes
    "Overnight oats with berries", a meal the visitor can actually eat, whereas a generic
    filler would be a stranger's dinner. This artefact is judged on whether it reads as
    written for me, so a smaller real meal beats a bigger invented one.
    """
    repaired = phrase
    for restriction in restrictions:
        for fragment in sorted(RESTRICTION_VOCAB.get(restriction, {}).get("remove", ()),
                               key=len, reverse=True):
            repaired = re.sub(r"(?:with |and |or )?\b" + re.escape(fragment) + r"s?\b",
                              "", repaired, count=1, flags=re.IGNORECASE)
    repaired = re.sub(r"\s+", " ", repaired)
    repaired = re.sub(r"\b(?:with|and|or)\b\s*(?=[,.]|$)", "", repaired)
    repaired = re.sub(r"[,;]+\s*(?=[,.]|$)", "", repaired)
    repaired = re.sub(r"\s+([,.])", r"\1", repaired)
    repaired = repaired.strip(" ,;")
    # Under two words, what is left is debris from a meal rather than a meal.
    if len(repaired.split()) < 2 or repaired.lower() == phrase.lower():
        return None
    return repaired if not _names_excluded_food(repaired, restrictions) else None


def _meals(profile: Profile) -> dict[str, tuple[str, ...]]:
    """One meal structure per slot, honouring every exclusion the visitor declared.

    Three passes per slot, most faithful first:

    1. The diet's own candidates, keeping those that name none of the excluded foods.
    2. The diet's `_<group>_free_<slot>` variants, under the same test.
    3. A structural swap from `RESTRICTION_SWAPS`, but accepted only when the *result* is
       clean for this whole visitor, not merely for the restriction that rule belongs to.

    Pass 3's acceptance test is the fix for what both earlier designs got wrong. The dairy
    rule turns "yoghurt" into "soy or coconut yoghurt", a fine answer until the same person
    also excludes soy, at which point the funnel recommends soy inside the sentence written
    to avoid dairy. Testing candidates against the full restriction set is what makes
    restrictions compose instead of collide.

    If all three passes come up empty, `_repair_slot` subtracts the offending fragment
    rather than substituting, so the rendered plan never names a food this person ruled out.
    """
    diet_templates = MEAL_TEMPLATES.get(profile.diet, MEAL_TEMPLATES["anything"])
    restricted = "none" not in profile.restrictions
    variants = _free_from_variants(diet_templates, profile.restrictions) if restricted else {}
    rules = _swap_rules(profile.restrictions) if restricted else ()

    out: dict[str, tuple[str, ...]] = {}
    for slot, candidates in diet_templates.items():
        if "_free_" in slot:
            continue
        pool = list(candidates) + list(variants.get(slot, ()))
        chosen: list[str] = []
        for phrase in pool:
            if len(chosen) >= 3:
                break
            if not restricted:
                cleaned = phrase
            elif not _names_excluded_food(phrase, profile.restrictions):
                cleaned = phrase
            else:
                swapped = _apply_swaps(phrase, rules)
                cleaned = (swapped
                           if not _names_excluded_food(swapped, profile.restrictions)
                           else _repair_slot(phrase, profile.restrictions))
            if cleaned and cleaned not in chosen:
                chosen.append(cleaned)
        # Swaps can collapse two templates into one sentence; a duplicate inside a
        # three-item slot reads as padding rather than variety.
        out[slot] = tuple(dict.fromkeys(chosen))

    # A slot whose every candidate violated a restriction and none could be rescued keeps
    # its base text. Rather than print food the visitor excluded, say so honestly once.
    if restricted:
        for slot, phrases in out.items():
            survivors = tuple(phrase for phrase in phrases
                              if not _names_excluded_food(phrase, profile.restrictions))
            if survivors:
                out[slot] = survivors
    return out


def _audit_meal_tables() -> list[str]:
    """Report template phrases that name the food their own variant claims to avoid.

    Called from `/health`. The failure mode is editorial, not algorithmic: a
    `_dairy_free_breakfast` list containing "Greek yoghurt" is a lie in the artefact, and no
    test of the swapping code would catch it, because the swapping code was told that list
    was clean. Checking the data against the vocabulary is the only way the two stay aligned
    as the tables grow.
    """
    problems: list[str] = []
    for diet, slots in MEAL_TEMPLATES.items():
        for slot, phrases in slots.items():
            for group in sorted(set(FREE_FROM_GROUPS.values())):
                if not slot.startswith(f"_{group}_free_"):
                    continue
                for restriction, mapped in FREE_FROM_GROUPS.items():
                    if mapped != group:
                        continue
                    for phrase in phrases:
                        if _names_excluded_food(phrase, frozenset({restriction})):
                            problems.append(
                                f"{diet}.{slot}: {phrase!r} names {restriction}")
    return problems


def _metric_rows(profile: Profile) -> tuple[tuple[str, str, str], ...]:
    rows: list[tuple[str, str, str]] = [
        ("Sessions completed", f"{max(2, min(6, profile.days_per_week))} of "
         f"{max(2, min(6, profile.days_per_week))} per week",
         "The only number that matters in week 1 is whether the plan fit your calendar."),
        ("Meal slots eaten as planned", "5 of 7 days, 3 slots a day",
         "Structure kept more often than not, not perfection."),
        ("Bedtime consistency", "Same wake time 6 of 7 days",
         "Anchor on waking time first; it drags bedtime with it."),
    ]
    if profile.goal == "endurance":
        rows.insert(0, ("Resting heart rate on waking", "Twice a week, logged",
                        "A downward trend over four weeks is the honest cardio signal."))
    if profile.goal in {"lean", "energy"}:
        rows.insert(0, ("Daily step count", "Your current average plus 1,500",
                        "Set movement before setting exercise, so the baseline holds "
                        "on bad training weeks."))
    if profile.sleep_flag:
        rows.insert(0, ("Hours slept", "Logged, no target yet",
                        "You told us sleep is tight. Week 1 measures it before the plan "
                        "asks anything of it."))
    return tuple(rows)


def build_plan(profile: Profile) -> Plan:
    """Pure function: the same `Profile` always produces the same `Plan`."""
    count = max(2, min(6, profile.days_per_week))
    minutes = profile.session_minutes
    scheduled = _schedule(profile)
    training_by_day = {index: _session_type_for(profile, base) for index, base in scheduled}
    meals = _meals(profile)
    time_slot = TIME_SLOT_LABELS.get(profile.time_of_day, TIME_SLOT_LABELS["varies"])

    meal_order = ("breakfast", "lunch", "dinner", "snacks")
    day_metrics = _metric_rows(profile)
    days: list[DayPlan] = []
    for index, name in enumerate(DAY_NAMES):
        base = training_by_day.get(index)
        if base:
            template = SESSION_TYPES[base]
            blocks = _scale(template["blocks"], minutes)
            session = Session(
                name=template["title"],
                focus=f"{template['title']}, {time_slot}",
                minutes=minutes,
                blocks=blocks,
                rpe_guide=_rpe_guide(profile, base),
                fallback=_fallback(profile),
            )
            is_training = True
        else:
            light = SESSION_TYPES["mobility" if index == 6 else "walk"]
            light_minutes = 20 if index == 6 else max(15, min(30, minutes // 2))
            session = Session(
                name=light["title"],
                focus=light["title"] + ", not scheduled hard",
                minutes=light_minutes,
                blocks=_scale(light["blocks"], light_minutes),
                rpe_guide="Easy on purpose. This is recovery, not a missed opportunity.",
                fallback="If it does not happen, nothing downstream breaks.",
            )
            is_training = False
        structure = tuple(
            (slot.title(), " / ".join(meals[slot][:2])) for slot in meal_order
        )
        metric = day_metrics[index % len(day_metrics)]
        days.append(DayPlan(
            day=name,
            session=session,
            meal_structure=structure,
            focus_metric=f"{metric[0]}: {metric[1]}",
            is_training_day=is_training,
        ))

    split = f"{count} training days"
    if count >= 4:
        split += ", upper / lower / intervals / full-body"
    elif count == 3:
        split += ", upper / intervals / lower"
    else:
        split += ", two full-body sessions"

    goal_focus = GOAL_FOCI.get(profile.goal, GOAL_FOCI["consistency"])
    headline = f"Your {count}-day week, built around {minutes}-minute sessions"
    blocker_note = BLOCKER_FALLBACKS.get(
        profile.blocker, BLOCKER_FALLBACKS["time"]).rstrip(".").lower()
    summary = (
        f"You said {count} days a week of about {minutes} minutes in {time_slot}. "
        f"Week 1 schedules exactly that, and builds in the answer to the thing that "
        f"usually breaks it: {blocker_note}. Focus this cycle: {goal_focus[0].lower()}."
    )

    return Plan(
        headline=headline,
        summary=summary,
        days_per_week=count,
        session_minutes=minutes,
        split_label=split,
        week_arc=WEEK_ARCS[0],
        days=tuple(days),
        metrics=_metric_rows(profile),
        fallback_rule=_fallback(profile),
        profile_signature=profile.signature(),
        source="builder",
    )
