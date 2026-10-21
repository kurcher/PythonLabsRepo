# findex — власна пошукова система

[![CI](https://github.com/kurcher/PythonLabsRepo/actions/workflows/ci.yml/badge.svg)](https://github.com/kurcher/PythonLabsRepo/actions/workflows/ci.yml)

Повнотекстовий пошук по корпусу, який будується впродовж 8 лабораторних курсу
[Python — Build a Search Engine](https://github.com/rmalkevy/Programming-Practice-Projects/tree/main/courses/python).
Кожна лаба позначена git-тегом `lab-01` … `lab-08`.

## Корпус

**Project Gutenberg, "Top 100"**: 107 найпопулярніших книжок (англійська переважно, плюс кілька
іспанських/португальських/французьких), порізаних на розділи → **5 229 документів, 113 МБ тексту,
17,5 млн токенів, словник 246 809 термів**.

Корпус у git не потрапляє (`data/` у `.gitignore`). Як відтворити:

```bash
uv run python scripts/fetch_gutenberg.py      # ~107 книжок у data/gutenberg/ (з дзеркала PG, 1 запит/с)
uv run python scripts/split_chapters.py       # data/chapters/<книга>/<NNN>.txt — один розділ = один документ
```

`fetch_gutenberg.py` бере список з `gutenberg.org/browse/scores/top` (один запит), а самі тексти —
з офіційного дзеркала (PG просить роботів не качати з головного сайту), прибирає ліцензійний
заголовок/футер PG. `split_chapters.py` ріже книжку по заголовках `CHAPTER/BOOK/PART/STAVE/LETTER …`,
а якщо їх < 5 — на шматки ~20 КБ по межах абзаців. Перший рядок файлу — `<назва книги> — <розділ>`.

---

## Lab 1 — потоки, а не списки

```bash
uv run python -m findex.stats data/chapters            # статистика всього корпусу
uv run python -m findex.stats data/chapters --limit 500 --eager
uv run pytest
```

* `findex.corpus.iter_documents(root)` — генератор `Document(doc_id, path, text)`. Відкриває один файл
  за раз (`rglob("*")`, сортовано, тож `doc_id` стабільні). Підтримує дерево `*.txt`, JSON-lines
  (`*.jsonl`, поле `text`, опційно `title`) і те саме в `gzip` без розпакування. Файл з неправильним
  кодуванням логується (`logging.warning`) і пропускається. `next(iter_documents(Path("data/chapters")))`
  повертається за ~40 мс — читається лише перший файл.
* `findex.tokenize.tokenize(text)` — генератор поверх `re.finditer`.
* `findex.stats` — один прохід: `Counter.update(tokenize(doc.text))` на кожен документ;
  `--limit N` через `itertools.islice`; час — `perf_counter`, пікова пам'ять — `tracemalloc`.

### Правила токенізатора

| Правило | Приклад |
|---|---|
| NFC, потім `casefold()` | `"Straße"` → `strasse`; `"cafe" + U+0301` (e + комбінований акут) == `"café"` |
| Слово = літери та цифри Unicode (`\w` без `_`); решта — роздільники | `snake_case` → `snake`, `case` |
| Комбіновані знаки, що лишились після NFC (наголос у кирилиці), видаляються | `за́мок` → `замок` |
| Апостроф `'` `’` `ʼ` **всередині** слова зберігається й уніфікується до `'`; на краях — це лапки, відкидаються | `don’t` → `don't`, `пʼять` → `п'ять`, `'quoted'` → `quoted` |
| Дефіс розділяє | `well-known` → `well`, `known` (запит токенізується так само, тож збіг є) |
| Цифри — токени; змішані літери+цифри — один токен | `1984`, `mp3`; `3.14` → `3`, `14` |

Тести: `tests/test_tokenize.py` (10 перевірок: регістр, `ß`, кирилиця, NFC з комбінованим знаком,
наголос, пунктуація, апострофи, дефіси, цифри, порожній рядок, і що `tokenize` — генераторна функція).

### Результат на повному корпусі

`python -m findex.stats data/chapters` → 5 229 документів, 17 511 543 токени, словник 246 809,
топ-3: `the` 839 787, `and` 535 892, `of` 492 844. Пікова пам'ять **43 МіБ** при 113 МБ корпусу.

### Eager vs lazy

Обидві версії — `python -m findex.stats data/chapters [--limit N] [--eager]` на одному й тому самому
корпусі (Windows 11, i5-12500H, 16 ГБ RAM, CPython 3.13.7; час — під увімкненим `tracemalloc`, який
сповільнює програму приблизно вдвічі, тож порівнювати варто відносно).

| Версія | Документів | Токенів | Пікова пам'ять | Час |
|---|---|---|---|---|
| eager (lists) | 500 | 1 639 128 | 111.7 МіБ | 8.8 с |
| lazy (generators) | 500 | 1 639 128 | 7.2 МіБ | 8.8 с |
| eager (lists) | 2 000 | 6 429 853 | 443.9 МіБ | 66.6 с |
| lazy (generators) | 2 000 | 6 429 853 | 11.0 МіБ | 38.9 с |
| eager (lists) | 5 229 (весь корпус) | 17 511 543 | **1 199.8 МіБ** | 159.4 с |
| lazy (generators) | 5 229 (весь корпус) | 17 511 543 | **43.3 МіБ** | 136.5 с |

**Куди пішла пам'ять eager-версії.** Майже вся вона — у токенах як окремих об'єктах `str`.
Кожен токен, який повертає `re.finditer`, — новий рядок: `sys.getsizeof("the") == 44` байти
(заголовок об'єкта ~41 Б + символи), для кирилиці ще більше (`"привіт"` — 70 Б). 17,5 млн токенів ×
~45 Б ≈ **790 МБ**. Зверху — два шари вказівників по 8 Б на токен: `list[list[str]]` по документах
і плаский список `flat` ≈ 2 × 140 МБ, плюс сам текст усіх документів у `list[Document]` ≈ 113 МБ
(ASCII-рядки в CPython — 1 байт на символ). Разом ≈ 1,2 ГБ, що й показує `tracemalloc`. Пам'ять
росте лінійно з корпусом (112 → 444 → 1 200 МіБ), тобто на 10× більшому корпусі eager-версія просто
не влізе в RAM.

**Чому lazy-версія не нуль.** У кожен момент живі лише: поточний документ (його текст, і копія після
NFC+`casefold`, бо `normalize` повертає новий рядок), один поточний токен і `Counter`. `Counter` і є
основна частина 43 МіБ: 246 809 унікальних ключів-рядків (~50 Б кожен) + об'єкти `int` для лічильників
+ сама хеш-таблиця словника (~таблиця індексів і записів по 24 Б). Він росте з розміром *словника*,
а не корпусу — тому 500 → 5 229 документів дає лише 7 → 43 МіБ (закон Гіпса: словник росте
сублінійно). Токен, щойно порахований `Counter.update`, втрачає останнє посилання й одразу
звільняється, тож ті самі 44 байти перевикористовуються для наступного.

**Чому lazy ще й швидша.** Eager створює ~17 млн об'єктів, які живуть одночасно: аллокатор постійно
просить нову пам'ять у ОС, а циклічний GC періодично обходить мільйони контейнерів у списках. Lazy
працює в «гарячому» кеші й перевикористовує ті самі блоки pymalloc.

---

## Lab 2 — інвертований індекс

```bash
uv run python -m findex.index data/chapters --out build/index.bin [--positions] [--format binary|pickle|json]
uv run python -m findex.search build/index.bin "whale OR leviathan NOT ship" [--engine merge|set]
uv run python scripts/bench_lab02.py memory|formats|engines ...   # усі таблиці нижче
```

* `findex.index.build_index(docs, positions=False)` — один потоковий прохід: для кожного документа
  рахується `Counter` (або `term → [позиції]`), одразу «зливається» у `defaultdict(list)` постингів і
  викидається. `doc_id` зростають, тому списки постингів відсортовані без окремого `sort`.
  Результат: `postings: dict[str, list[Posting]]`, `doc_lengths: dict[int, int]`, `doc_meta: dict[int, DocMeta]`.
* `Posting(doc_id, tf, positions=())` і `DocMeta(doc_id, path, title)` — `@dataclass(frozen=True, slots=True)`,
  тобто хешовані (можна класти в `set`, тест `test_dataclasses_are_hashable_and_frozen`) і без `__dict__`.
* `findex.search` — запит `a b OR c NOT d` обчислюється зліва направо; AND/OR/NOT — двопоказникові
  злиття (`intersect`/`union`/`difference`) по відсортованих `doc_id`; `--engine set` — те саме через `set`.
* `findex.store` — `save`/`load` у трьох форматах. **pickle небезпечний**: `pickle.load` викликає
  довільні callables з файлу (`__reduce__` → `os.system(...)`), тож завантажений з інтернету `.pkl`
  = віддалене виконання коду. Тому за замовчуванням — власний `binary`: JSON-заголовок
  (документи + `term → [offset, count, npos]`) і далі один суцільний `array('I')` з doc_ids, tf і позиціями.
  Його завантаження лише розбирає дані.

Корпус: 5 229 документів, 246 809 термів, **4 979 580 постингів**.

### Пам'ять постингів (головна таблиця)

`tracemalloc`, побудова лише постингів на всьому корпусі, кожен варіант в окремому процесі:

| Представлення постингів | Пікова пам'ять (build) | Живе після build | Файл (pickle) | Завантаження |
|---|---|---|---|---|
| `list[Posting]`, звичайний `@dataclass` | 541 МіБ | 534 МіБ | 107.7 МіБ | 13.4 с |
| `list[Posting]`, `slots=True` | 351 МіБ | 344 МіБ | 79.2 МіБ | 20.9 с |
| `array('I')` пари (doc_ids, tfs), без об'єктів | **129 МіБ** | 115 МіБ | **48.6 МіБ** | **0.8 с** |

**Куди пішли байти.** Спільне для всіх рядків — ~246 тис. ключів-рядків словника + хеш-таблиця
(≈ 30–40 МіБ, як і `Counter` у Lab 1). Решта — постинги, ~5 млн штук:

* **plain dataclass**: кожен `Posting` — об'єкт (16 Б заголовок + 8 Б вказівник на `__dict__` + weakref)
  ≈ 48 Б, плюс власний `__dict__` (~100+ Б на ключі `doc_id/tf/positions` у split-table), плюс 8 Б
  вказівника у списку. ≈ 100 Б на постинг → ~500 МіБ. `int` до 256 кешовані, тож tf майже нічого не коштує.
* **slots**: `__dict__` зникає, атрибути лежать у фіксованих слотах прямо в об'єкті: 16 Б заголовок
  + 3 × 8 Б слотів = 40 Б (+GC-заголовок 16 Б) + 8 Б у списку ≈ 64 Б → ~310 МіБ. Ціна: не можна
  додавати нові атрибути, складніше множинне успадкування.
* **array('I')**: жодних об'єктів на постинг — лише 4 Б doc_id + 4 Б tf у суцільному C-буфері = 8 Б.
  Виграш у ~8× на постинг (64 → 8 Б). pickle масиву — це просто сирі байти, тому файл менший, а
  завантаження — це `memcpy`, а не створення 5 млн об'єктів (звідси 0.8 с проти 13–21 с).
  Чому slots-pickle вантажиться *повільніше* за plain: для slots-об'єктів pickle відновлює стан через
  `__setstate__`/`setattr` по кожному слоту, а для plain — одним `__dict__.update`.

### Формати збереження

Повний індекс (`InvertedIndex` з `list[Posting]`, без tracemalloc):

| Формат | Позиції | Розмір файлу | save | load |
|---|---|---|---|---|
| binary | ні | **44.7 МіБ** | **2.8 с** | **25.3 с** |
| pickle | ні | 80.0 МіБ | 29.6 с | 42.1 с |
| json | ні | 45.9 МіБ | 42.5 с | 32.0 с |
| binary | так | 111.7 МіБ | 7.2 с | 25.4 с |
| pickle | так | 134.9 МіБ | 34.3 с | 47.6 с |
| json | так | 134.8 МіБ | 79.9 с | 35.2 с |

Позиції (`--positions`) збільшують binary-індекс у **2.5×** (44.7 → 111.7 МіБ): на кожне входження
токена — ще 4 Б. Load у всіх форматах домінує одне й те саме — створення ~5 млн об'єктів `Posting`;
binary виграє, бо читає числа з `array` замість розбору тексту (json) чи виклику конструкторів через
pickle-опкоди. (У Lab 8 постинги стануть NumPy-масивами — тоді load буде як у рядку `array` вище.)

### merge vs set

`scripts/bench_lab02.py engines build/index.bin`, найкращий з 5 × 20 повторів. Найчастіші терми
корпусу — `a` і `as` (не `the`: іспанські/португальські книжки його не містять):

| Запит | Збігів | merge | set |
|---|---|---|---|
| `as a` (AND, 2 найчастіші) | 5 034 | 0.96 мс | 0.63 мс |
| `as OR a` | 5 178 | 0.98 мс | 0.67 мс |
| `tillest tubalcain` (2 найрідші) | 1 | 0.007 мс | 0.007 мс |
| `as tillest` (частий + рідкий) | 1 | **0.10 мс** | 0.19 мс |

Сама операція на готових відсортованих списках:

| Списки | merge `intersect` | `set &` (множини вже є) | `sorted(set(a) & set(b))` |
|---|---|---|---|
| 5 038 × 5 174 | 756 мкс | 160 мкс | 430 мкс |
| 5 038 × 1 | 0.5 мкс | 0.2 мкс | 86 мкс |

Обидва — O(n + m), але merge — це цикл інтерпретатора (байткод на кожен крок), а `set &` — цикл
у C, тому на двох довгих списках set швидший у ~2–4×. Зате merge не будує хеш-таблиць: коли один
список короткий, двопоказниковий цикл закінчується, щойно вичерпано короткий список (0.5 мкс),
а set-варіант усе одно хешує всі 5 038 елементів довгого (86 мкс). На рідкісних термах різниці
немає — домінує накладна ціна виклику. Висновок: сортовані списки + merge — правильна структура
для диска й для асиметричних запитів; `set` — коли обидва списки довгі й уже в пам'яті.

---

## Lab 3 — ранжування та об'єктна модель

```bash
uv run python -m findex.search build/index-pos.bin 'white whale' '"call me ishmael"' 'white whale' -k 3 -v
uv run python -m findex.search build/index.bin 'whale NOT ship' --scorer tfidf
uv run python scripts/eval_lab03.py build/index-pos.bin
```

* **`Index(Mapping[str, list[Posting]])`** (`findex/index.py`): `__getitem__` (KeyError для
  невідомого терму), `__len__` (кількість термів), `__iter__`, `__contains__`, `__repr__` →
  `<Index 246,809 terms, 5,229 docs, positions>`; `num_docs`, `avg_doc_length` (`cached_property`),
  `doc_length()`, `df()`, `title()`, `text()`. `Mapping` безкоштовно дає `get/keys/items/values/==`.
  `open_index(path)` — `@contextmanager`; `finally` викликає `index.close()` навіть при винятку
  (тест `test_open_index_releases_on_exception`).
* **`Scorer`** (`findex/scoring.py`) — `typing.Protocol` з `__call__(term, posting, index) -> float`;
  `TfIdf` = `(1 + log tf) · log(N/df)` і `BM25(k1=1.5, b=0.75)` — frozen dataclasses з `__call__`.
  `search(index, query, scorer=BM25(), k=10)` накопичує `score[doc_id]` по постингах позитивних
  термів запиту та бере топ-k через `heapq.nlargest` (O(n log k)). Результат — `SearchResult(score,
  doc_id, title, snippet)` з `order=True`; `score` стоїть першим полем, тож `sorted(results, reverse=True)`
  — це ранжування (`title`/`snippet` мають `compare=False`).
* **Мова запитів** (`findex/query.py`) — рекурсивний спуск: `or_expr → and_expr ('OR' and_expr)*`,
  `and_expr → not_expr ('AND'? not_expr)*`, `not_expr → 'NOT' not_expr | atom`,
  `atom → слово | "фраза" | '(' or_expr ')'`. Вузли `Term/Phrase/And/Or/Not` — frozen dataclasses,
  перевантажують `&`, `|`, `~`, мають `evaluate(index) -> frozenset[int]`. `And(x, Not(y))`
  обчислюється як різниця без матеріалізації «всі документи мінус y». Фрази — через позиції:
  документ підходить, якщо існує `p` з `p+i` у позиціях i-го слова. Тести порівнюють *дерева*:
  `parse("a OR b c") == Or(Term("a"), And(Term("b"), Term("c")))`.
* **`@timed`** (`findex/decorators.py`, з `functools.wraps`, PEP 695 `[**P, R]`) на `build_index`,
  `store.load`, `search` — пише `INFO findex.timing: search took 39.11 ms` у лог (`-v`).
* **`lru_cache(maxsize=256)`** на шляху «рядок запиту → множина doc_id» (`Index.matching`). Він
  пер-інстансний, бо `Index` нехешований (`Mapping` ставить `__hash__ = None`), тож його не можна
  передати в модульну кешовану функцію. Видно в лозі на повторному запиті:
  ```
  DEBUG matching cache: CacheInfo(hits=0, misses=1, ...)   # 'white whale'      — 39.1 ms
  DEBUG matching cache: CacheInfo(hits=0, misses=2, ...)   # '"call me ishmael"'
  DEBUG matching cache: CacheInfo(hits=1, misses=2, ...)   # 'white whale' знов — 28.9 ms
  ```
  (решта часу — скоринг і читання файлів для сніпетів).
* **Сніпети** (`findex/snippets.py`): вікно ~160 символів з найбільшою кількістю *різних* термів
  запиту, межі вирівняні по словах, збіги підсвічені (`**…**`, ANSI у терміналі, `<mark>` для веба в Lab 7).
  Пошук збігів іде по NFC-тексту без casefold, тож регістр оригіналу зберігається:
  ```
  1.  10.334  [3413] Moby Dick; Or, The Whale by Herman Melville — CHAPTER 41. Moby Dick.
       …intervals only, the unaccompanied, secluded **White** **Whale** had haunted those uncivilized seas…
  ```

### Три перевірки ранжування на корпусі

(N = 5 229, середня довжина документа 3 349 токенів.)

1. **Рідкісний терм важить більше.** `harpoon` (df = 39) з tf = 1 дає BM25 **6.55** (TF-IDF 4.90), а
   `the` (df = 4 915) навіть з tf = 423 — лише **0.15** (TF-IDF 0.44). Запит `the OR harpoon` у
   топ-3 обох скорерів — тільки розділи «Moby Dick» із гарпуном.
2. **Насичення tf у BM25.** Внесок `whale` у документ середньої довжини:

   | tf | 1 | 2 | 3 | 5 | 10 | 19 | 20 |
   |---|---|---|---|---|---|---|---|
   | BM25 | 3.31 | 4.73 | 5.51 | 6.36 | 7.19 | 7.66 | **7.69** |
   | TF-IDF | 3.31 | 5.60 | 6.95 | 8.64 | 10.93 | 13.06 | 13.23 |

   20-те входження додає до BM25 лише +0.03 (друге додало +1.42); межа — `idf·(k1+1)`.
3. **Нормалізація довжини.** `harpoon` з tf = 1 у 445-токенному розділі «Moby Dick» (гл. 131) —
   BM25 **8.01**; у 155 095-токенному шматку каталогу «Harrods for Everything» — **0.23**.
   TF-IDF дає обом однакові 4.90. Ці ж три властивості перевіряються тестами на фікстурі
   (`tests/test_ranking.py`).

### precision@5: TF-IDF vs BM25

10 запитів, мітки релевантності визначені заздалегідь як «книжка(и), яку хоче бачити користувач»
(`scripts/eval_lab03.py`). Неявний AND іноді дає < 5 збігів — бракуючі позиції рахуються як нерелевантні.

| Запит | TF-IDF P@5 | top-5 | BM25 P@5 | top-5 |
|---|---|---|---|---|
| `white whale` | 1.0 | ✓✓✓✓✓ | 1.0 | ✓✓✓✓✓ |
| `ghost of christmas past` | 0.8 | ✓✓✓✓✗ | 0.8 | ✓✓✓✓✗ |
| `vampire blood count castle` | 0.6 | ✓✓✓✗ | 0.6 | ✓✓✓✗ |
| `sherlock holmes` | 1.0 | ✓✓✓✓✓ | 1.0 | ✓✓✓✓✓ |
| `elizabeth darcy pemberley` | 1.0 | ✓✓✓✓✓ | 1.0 | ✓✓✓✓✓ |
| `natural selection species` | 1.0 | ✓✓✓✓✓ | 1.0 | ✓✓✓✓✓ |
| `rabbit hole` | 0.4 | ✗✗✗✓✓ | 0.4 | ✗✓✗✓✗ |
| `monster creator` | 0.6 | ✗✓✓✓✗ | 0.8 | ✗✓✓✓✓ |
| `treasure map pirates` | 0.2 | ✓✗✗ | 0.2 | ✓✗✗ |
| `war strategy enemy general` | 1.0 | ✓✓✓✓✓ | 1.0 | ✓✓✓✓✓ |
| **середнє** | **0.76** | | **0.78** | |

BM25 трохи краще там, де TF-IDF «тягне» довгі документи з багатьма повтореннями (`monster creator` —
енциклопедія Nuttall і повне зібрання Шекспіра). Слабкі місця обох — короткі запити з загальними
словами (`rabbit hole`) і жорсткий AND (`treasure map pirates` має лише 3 збіги: слова `pirates`
у «Treasure Island» майже немає — там `buccaneers`). Це якраз мотивація для семантичного пошуку в Lab 8.

---

## Lab 4 — справжній інструмент: типи, тести, пакет, CLI

### Встановлення

```bash
# з GitHub Release (wheel)
uv tool install https://github.com/kurcher/PythonLabsRepo/releases/download/v0.4.0/findex-0.4.0-py3-none-any.whl
# або з вихідного коду
uv tool install .            # кладе `findex` у PATH
uv build                     # dist/findex-0.4.0-py3-none-any.whl + sdist
```

### Команди

```bash
findex index data/chapters --out build/index.bin [--positions] [--limit N] [--format binary|pickle|json]
findex search build/index.bin 'white whale NOT ship' [-k 10] [--scorer bm25|tfidf] [--json]
findex stats build/index.bin [--top 20]
findex -v search ...    # INFO-логи (час @timed);  -vv — DEBUG (cache_info)
```

* `typer`-застосунок (`findex/cli.py`), `[project.scripts] findex = "findex.cli:app"`; також `python -m findex`.
* `rich`: прогрес-бар під час `index`, таблиця результатів із підсвіченими сніпетами в `search`,
  таблиці в `stats`. Текст документів екранується (`rich.markup.escape`) *до* вставки розмітки
  підсвітки, тож `[bold]` у книжці не зламає вивід.
* `--json` — по одному JSON-об'єкту на рядок у **stdout** (`findex search ix.bin whale --json | jq .title`);
  усе діагностичне (логи `RichHandler`, прогрес, помилки) — у **stderr**.
* `logging` налаштовується один раз — у callback кореневої команди (`-v` → INFO, `-vv` → DEBUG).
* Очікувані помилки — один рядок і ненульовий код: неіснуючий індекс → `error: index file not found: …`,
  exit 1; синтаксична помилка запиту → `error: bad query: …`, exit 2; помилки аргументів — exit 2 (typer).

### Типи

`pyright --strict` (`[tool.pyright] typeCheckingMode = "strict"`, src + tests): **0 errors**, жодного
`# type: ignore` у `src/`. `Scorer` — `typing.Protocol` (BM25/TfIdf задовольняють його структурно, без
наслідування); генератори анотовані як `Iterator[...]`, аргументи — `Iterable[...]`; `Literal` для
`--scorer`/`--format`/`--engine`; PEP 695 у `timed[**P, R]` і `type SetOp = ...`;
`@contextmanager` повертає `Generator[Index]`.

### Тести

`uv run pytest --cov` — **61 тест**, з них **6 Hypothesis-властивостей** (`tests/test_properties.py`):

1. двопоказникові `intersect/union/difference` == відповідні операції над `set` для будь-яких відсортованих списків;
2. їхній вихід завжди відсортований і без дублікатів;
3. кожен токен — нерухома точка: NFC-нормалізований і `tokenize(tok) == [tok]`;
4. `tokenize(" ".join(tokenize(x))) == tokenize(x)` для довільного Unicode-тексту;
5. `save → load` зберігає індекс для довільних корпусів × позиції так/ні × 3 формати (`@pytest.mark.slow`);
6. дерево запиту обчислюється як алгебра множин: `a b` = A∩B, `a OR b` = A∪B, `a NOT b` = A∖B, `NOT a` = U∖A.

`conftest.py` дає session-фікстури: корпус з 8 крихітних документів (`tests/fixtures/corpus/`),
побудований з нього індекс і збережений файл індексу. Решта: токенізатор (Unicode, апострофи,
дефіси, цифри), корпус (jsonl, gzip, битий UTF-8 → лог і пропуск), індекс, злиття, парсер (рівність
дерев), три перевірки ранжування з Lab 3, кеш, сніпети, round-trip форматів, CLI через `typer.testing.CliRunner`
(включно з тим, що `-vv` пише в stderr, а stdout лишається чистим JSON).

**Покриття: 87 %** (branch coverage). Непокриті місця свідомо:
`index.main`/`search.main` — старі argparse-CLI з Lab 2 (замінені `findex`, лишені для таблиць Lab 2);
`memory.peak_rss` — гілки для конкретної ОС (на Windows не виконується Linux-гілка і навпаки);
`__main__.py` — дві строки-обгортки.

### CI і реліз

`.github/workflows/ci.yml`: на кожен push/PR — `uv sync --locked` → `ruff check` → `ruff format --check`
→ `pyright` → `pytest --cov` → `uv build` (wheel як артефакт). Реліз **v0.4.0** з wheel:
https://github.com/kurcher/PythonLabsRepo/releases/tag/v0.4.0
