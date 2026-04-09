#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ZIM_DIR="$PROJECT_DIR/zim"

# Shared progress + error log helpers. Writes data/downloads/state.json so
# the library page can show live progress even across navigations, and
# data/downloads/errors.jsonl so bad catalog URLs are queryable.
# shellcheck source=lib/download-log.sh
source "$SCRIPT_DIR/lib/download-log.sh"
export KRULL_DOWNLOAD_KIND=knowledge

echo "Knowledge Base Downloader for Krull AI"
echo ""

# --- Catalog ---
# Each entry: KEY|FILE|DESCRIPTION|SIZE
CATALOG=(
    # Developer Documentation (DevDocs)
    "devdocs-python|devdocs/devdocs_en_python_2026-02.zim|Python standard library docs|4 MB"
    "devdocs-javascript|devdocs/devdocs_en_javascript_2026-01.zim|JavaScript reference|3 MB"
    "devdocs-typescript|devdocs/devdocs_en_typescript_2026-01.zim|TypeScript reference|3 MB"
    "devdocs-node|devdocs/devdocs_en_node_2026-01.zim|Node.js docs|5 MB"
    "devdocs-react|devdocs/devdocs_en_react_2026-02.zim|React docs|3 MB"
    "devdocs-docker|devdocs/devdocs_en_docker_2026-01.zim|Docker documentation|2 MB"
    "devdocs-kubernetes|devdocs/devdocs_en_kubernetes_2026-01.zim|Kubernetes docs|1 MB"
    "devdocs-git|devdocs/devdocs_en_git_2026-01.zim|Git reference|2 MB"
    "devdocs-rust|devdocs/devdocs_en_rust_2026-01.zim|Rust documentation|8 MB"
    "devdocs-go|devdocs/devdocs_en_go_2026-01.zim|Go documentation|4 MB"
    "devdocs-bash|devdocs/devdocs_en_bash_2026-01.zim|Bash reference|1 MB"
    "devdocs-css|devdocs/devdocs_en_css_2026-01.zim|CSS reference|8 MB"
    "devdocs-html|devdocs/devdocs_en_html_2026-01.zim|HTML reference|3 MB"
    "devdocs-svg|devdocs/devdocs_en_svg_2026-01.zim|SVG reference|1 MB"
    "devdocs-nextjs|devdocs/devdocs_en_nextjs_2026-01.zim|Next.js framework docs|1 MB"
    "devdocs-tailwindcss|devdocs/devdocs_en_tailwindcss_2026-02.zim|Tailwind CSS docs|1 MB"
    "devdocs-fastapi|devdocs/devdocs_en_fastapi_2026-01.zim|FastAPI framework docs|3 MB"
    "devdocs-vite|devdocs/devdocs_en_vite_2026-01.zim|Vite build tool docs|1 MB"
    "devdocs-nginx|devdocs/devdocs_en_nginx_2026-01.zim|Nginx web server docs|1 MB"
    "devdocs-php|devdocs/devdocs_en_php_2026-02.zim|PHP documentation|15 MB"
    "devdocs-phpunit|devdocs/devdocs_en_phpunit_2026-02.zim|PHPUnit testing framework|2 MB"
    "devdocs-mariadb|devdocs/devdocs_en_mariadb_2026-01.zim|MariaDB/MySQL docs|10 MB"
    "devdocs-postgresql|devdocs/devdocs_en_postgresql_2026-01.zim|PostgreSQL docs|15 MB"
    "devdocs-sqlite|devdocs/devdocs_en_sqlite_2026-01.zim|SQLite docs|2 MB"
    "devdocs-redis|devdocs/devdocs_en_redis_2026-01.zim|Redis docs|3 MB"
    "devdocs-numpy|devdocs/devdocs_en_numpy_2026-01.zim|NumPy docs|5 MB"
    "devdocs-pandas|devdocs/devdocs_en_pandas_2026-01.zim|Pandas docs|12 MB"
    "devdocs-scikit|devdocs/devdocs_en_scikit-learn_2026-01.zim|Scikit-learn docs|54 MB"

    # Stack Exchange
    "stackexchange-unix|stack_exchange/unix.stackexchange.com_en_all_2026-02.zim|Unix & Linux Q&A|1.2 GB"
    "stackexchange-ubuntu|stack_exchange/askubuntu.com_en_all_2025-12.zim|Ask Ubuntu Q&A|2.6 GB"
    "stackexchange-codereview|stack_exchange/codereview.stackexchange.com_en_all_2026-02.zim|Code Review Q&A|525 MB"
    "stackexchange-security|stack_exchange/security.stackexchange.com_en_all_2026-02.zim|Information Security Q&A|420 MB"
    "stackexchange-serverfault|stack_exchange/serverfault.com_en_all_2026-02.zim|Server administration Q&A|1.5 GB"
    "stackexchange-superuser|stack_exchange/superuser.com_en_all_2026-02.zim|Computer hardware & software Q&A|3.7 GB"
    "stackexchange-softeng|stack_exchange/softwareengineering.stackexchange.com_en_all_2026-02.zim|Software engineering Q&A|457 MB"
    "stackoverflow|stack_exchange/stackoverflow.com_en_all_2023-11.zim|Full Stack Overflow archive|75 GB"
    "stackexchange-cooking|stack_exchange/cooking.stackexchange.com_en_all_2026-02.zim|Seasoned Advice cooking Q&A|237 MB"
    "stackexchange-outdoors|stack_exchange/outdoors.stackexchange.com_en_all_2026-02.zim|Outdoors & wilderness Q&A|142 MB"
    "stackexchange-gardening|stack_exchange/gardening.stackexchange.com_en_all_2026-02.zim|Gardening & landscaping Q&A|925 MB"
    "stackexchange-linguistics|stack_exchange/linguistics.stackexchange.com_en_all_2026-02.zim|Linguistics Q&A (Sapir-Whorf, sociolinguistics, phonology)|82 MB"
    "stackexchange-psychology|stack_exchange/psychology.stackexchange.com_en_all_2026-02.zim|Psychology & Cognitive Science Q&A|64 MB"
    "stackexchange-latin|stack_exchange/latin.stackexchange.com_mul_all_2026-02.zim|Latin Stack Exchange — grammar, translation, classical philology Q&A|54 MB"
    "stackexchange-literature|stack_exchange/literature.stackexchange.com_en_all_2026-02.zim|Literature Stack Exchange — textual analysis and interpretation Q&A|68 MB"
    "stackexchange-history|stack_exchange/history.stackexchange.com_en_all_2026-02.zim|History Stack Exchange — sourcing, historiography, period-specific Q&A|304 MB"
    "stackexchange-hsm|stack_exchange/hsm.stackexchange.com_en_all_2026-02.zim|History of Science and Mathematics Stack Exchange|49 MB"
    "stackexchange-law|stack_exchange/law.stackexchange.com_en_all_2026-02.zim|Law Stack Exchange — doctrinal, comparative, procedural Q&A|176 MB"
    "stackexchange-music|stack_exchange/music.stackexchange.com_en_all_2026-02.zim|Music Stack Exchange — theory, performance, composition Q&A|323 MB"
    "stackexchange-philosophy|stack_exchange/philosophy.stackexchange.com_en_all_2026-02.zim|Philosophy Stack Exchange — epistemology, metaphysics, ethics Q&A|198 MB"
    "stackexchange-hinduism|stack_exchange/hinduism.stackexchange.com_en_all_2026-02.zim|Hinduism Stack Exchange — scripture, doctrine, practice Q&A|194 MB"
    "stackexchange-buddhism|stack_exchange/buddhism.stackexchange.com_en_all_2026-02.zim|Buddhism Stack Exchange — scripture, doctrine, practice Q&A|78 MB"
    "stackexchange-judaism|stack_exchange/judaism.stackexchange.com_en_all_2026-02.zim|Mi Yodeya — Judaism Stack Exchange: halacha, Torah, Talmud Q&A|280 MB"
    "stackexchange-christianity|stack_exchange/christianity.stackexchange.com_en_all_2026-02.zim|Christianity Stack Exchange — scripture, doctrine, denominational practice Q&A|189 MB"
    "stackexchange-islam|stack_exchange/islam.stackexchange.com_en_all_2026-02.zim|Islam Stack Exchange — Quran, hadith, fiqh, doctrinal practice Q&A|89 MB"
    "stackexchange-biology|stack_exchange/biology.stackexchange.com_en_all_2026-02.zim|Biology Stack Exchange — research-level life sciences Q&A|403 MB"
    "stackexchange-chemistry|stack_exchange/chemistry.stackexchange.com_en_all_2026-02.zim|Chemistry Stack Exchange — mechanism, synthesis, spectroscopy Q&A|397 MB"
    "stackexchange-cs|stack_exchange/cs.stackexchange.com_en_all_2026-02.zim|Computer Science Stack Exchange — applied and theoretical CS Q&A|264 MB"
    "stackexchange-cstheory|stack_exchange/cstheory.stackexchange.com_en_all_2026-02.zim|Theoretical Computer Science Stack Exchange — complexity, algorithms, logic|71 MB"
    "stackexchange-earthscience|stack_exchange/earthscience.stackexchange.com_en_all_2026-02.zim|Earth Science Stack Exchange — geology, meteorology, oceanography Q&A|126 MB"
    "stackexchange-engineering|stack_exchange/engineering.stackexchange.com_en_all_2026-02.zim|Engineering Stack Exchange — mechanical, civil, materials Q&A|242 MB"
    "stackexchange-electronics|stack_exchange/electronics.stackexchange.com_en_all_2026-02.zim|Electrical Engineering Stack Exchange — circuits, signal processing, embedded|3.89 GB"
    "stackexchange-mattermodeling|stack_exchange/mattermodeling.stackexchange.com_en_all_2026-02.zim|Matter Modeling Stack Exchange — computational materials science, DFT, MD|46 MB"
    "stackexchange-math|stack_exchange/math.stackexchange.com_en_all_2026-02.zim|Mathematics Stack Exchange — the largest math Q&A archive|6.9 GB"
    "stackexchange-physics|stack_exchange/physics.stackexchange.com_en_all_2026-02.zim|Physics Stack Exchange — canonical research-level physics Q&A|1.7 GB"
    "stackexchange-stats|stack_exchange/stats.stackexchange.com_en_all_2026-02.zim|Cross Validated — canonical statistics, ML, and data analysis Q&A|1.5 GB"
    "stackexchange-medicalsciences|stack_exchange/medicalsciences.stackexchange.com_en_all_2026-02.zim|Medical Sciences Stack Exchange — clinical and biomedical Q&A|58 MB"
    "stackexchange-economics|stack_exchange/economics.stackexchange.com_en_all_2026-02.zim|Economics Stack Exchange — theory and applied Q&A|109 MB"
    "stackexchange-academia|stack_exchange/academia.stackexchange.com_en_all_2026-02.zim|Academia Stack Exchange — academic career, teaching and higher ed Q&A|279 MB"
    "stackexchange-politics|stack_exchange/politics.stackexchange.com_en_all_2026-02.zim|Politics Stack Exchange — policy, governance, international relations Q&A|199 MB"

    # Survival & Self-Sufficiency
    "post-disaster|other/zimgit-post-disaster_en_2024-05.zim|Post-disaster survival library|645 MB"
    "field-medicine|other/zimgit-medicine_en_2024-08.zim|Field & emergency medicine library|70 MB"
    "military-medicine|zimit/fas-military-medicine_en_2025-06.zim|FAS military medicine library|81 MB"
    "water|other/zimgit-water_en_2024-08.zim|Water purification & sourcing|21 MB"
    "appropedia|other/appropedia_en_all_maxi_2026-02.zim|Sustainability & appropriate tech|582 MB"
    "energypedia|other/energypedia_en_all_maxi_2025-12.zim|Off-grid energy knowledge|799 MB"
    "wikivoyage|wikivoyage/wikivoyage_en_all_nopic_2026-03.zim|Travel & geography (no images)|232 MB"
    "ifixit|ifixit/ifixit_en_all_2025-12.zim|iFixit repair guides|3.5 GB"

    # Cooking
    "food-preparation|other/zimgit-food-preparation_en_2025-04.zim|Curated recipes & food prep techniques|98 MB"
    "foss-cooking|zimit/foss.cooking_en_all_2026-02.zim|FOSS Cooking recipes|24 MB"
    "public-domain-recipes|zimit/publicdomainrecipes.com_en_all_2026-02.zim|Public domain recipe collection|23 MB"
    "grimgrains|zimit/grimgrains_en_all_2026-02.zim|GrimGrains vegan recipes|25 MB"
    "based-cooking|zimit/based.cooking_en_all_2026-02.zim|Based Cooking recipes|16 MB"

    # Linux
    "archlinux|other/archlinux_en_all_maxi_2025-09.zim|Arch Linux Wiki|30 MB"

    # Reference
    "wiktionary|wiktionary/wiktionary_en_all_nopic_2026-02.zim|English dictionary & thesaurus|8.2 GB"
    "wikipedia-history|wikipedia/wikipedia_en_history_nopic_2026-01.zim|Wikipedia — History topic subset (no images)|605 MB"
    "wikipedia-molcell|wikipedia/wikipedia_en_molcell_nopic_2026-01.zim|Wikipedia — Molecular and cell biology subset (no images)|340 MB"
    "wikipedia-chemistry|wikipedia/wikipedia_en_chemistry_nopic_2026-01.zim|Wikipedia — Chemistry subset (no images)|107 MB"
    "wikipedia-mathematics|wikipedia/wikipedia_en_mathematics_nopic_2026-03.zim|Wikipedia — Mathematics subset (no images)|325 MB"
    "wikipedia-physics|wikipedia/wikipedia_en_physics_nopic_2026-01.zim|Wikipedia — Physics subset (no images)|292 MB"
    "wikipedia-geography|wikipedia/wikipedia_en_geography_nopic_2026-01.zim|Wikipedia — Geography subset (no images)|489 MB"
    "wikipedia-sociology|wikipedia/wikipedia_en_sociology_nopic_2026-01.zim|Wikipedia — Sociology subset (no images)|198 MB"
    "libretexts-biology|libretexts/libretexts.org_en_bio_2025-01.zim|LibreTexts — Biology open textbooks (molecular, cellular, organismal, ecology)|2.09 GB"
    "libretexts-chemistry|libretexts/libretexts.org_en_chem_2025-01.zim|LibreTexts — Chemistry open textbooks (general, organic, inorganic, physical, analytical)|2.03 GB"
    "libretexts-engineering|libretexts/libretexts.org_en_eng_2025-01.zim|LibreTexts — Engineering open textbooks (mechanical, civil, electrical, chemical)|647 MB"
    "libretexts-mathematics|libretexts/libretexts.org_en_math_2026-01.zim|LibreTexts — Mathematics open textbooks (analysis, algebra, topology, applied)|792 MB"
    "libretexts-physics|libretexts/libretexts.org_en_phys_2026-01.zim|LibreTexts — Physics open textbooks (classical, quantum, thermal, modern)|534 MB"
    "libretexts-statistics|libretexts/libretexts.org_en_stats_2026-01.zim|LibreTexts — Statistics open textbooks (probability, inference, applied)|206 MB"
    "libretexts-medicine|libretexts/libretexts.org_en_med_2025-01.zim|LibreTexts — Medicine open textbooks (anatomy, physiology, pathology, pharmacology)|1.11 GB"
    "libretexts-k12|libretexts/libretexts.org_en_k12_2026-01.zim|LibreTexts — K-12 open educational resources|353 MB"
    "planetmath|zimit/planetmath.org_en_all_2026-02.zim|PlanetMath — community mathematics encyclopedia with formal definitions and proofs|38 MB"
    "algorithms-erickson|zimit/jeffe.cs.illinois.edu_en_all_2026-03.zim|Jeff Erickson — Algorithms textbook and lecture notes (UIUC)|493 MB"
    "learningstats-r|zimit/learningstatisticswithr.com_en_all_2026-02.zim|Learning Statistics with R — Danielle Navarro's open textbook with R practicum|15 MB"
    "surviving-residency|videos/quickguidesformedicine_en_all_2025-12.zim|Surviving Residency — quick clinical guides for medical trainees|503 MB"
    "librepathology|other/librepathology_en_all_maxi_2025-09.zim|Libre Pathology — open pathology knowledge base|76 MB"
    "wikem|other/wikem_en_all_maxi_2021-02.zim|WikEM — open-access emergency medicine reference|42 MB"
    "mutopia-project|zimit/mutopiaproject.org_en_2025-09.zim|Mutopia Project — 2100+ free classical music scores|6.19 GB"
    "open-music-theory|zimit/openmusictheory.com_en_all_2026-03.zim|Open Music Theory — open-access theory textbook|78 MB"
    "internet-encyclopedia-philosophy|zimit/internet-encyclopedia-philosophy_en_all_2025-11.zim|Internet Encyclopedia of Philosophy — peer-reviewed academic reference|122 MB"
    "encyclopedia-environment|zimit/encyclopedie-environnement.org_en_2025-06.zim|Encyclopedia of the Environment — peer-reviewed environmental science|4.81 GB"
    "cd3wd|zimit/cd3wdproject.org_en_all_2025-11.zim|CD3WD — sustainable development and appropriate technology archive|554 MB"

    # Anthropology & Humanities
    "ted-anthropology|ted/ted_mul_anthropology_2026-01.zim|TED — Anthropology (talks & lectures)|635 MB"
    "ted-archaeology|ted/ted_mul_archaeology_2026-01.zim|TED — Archaeology (dig sites, methodology)|301 MB"
    "ted-asia|ted/ted_mul_asia_2026-01.zim|TED — Asia (talks on Asian societies, politics, culture)|572 MB"
    "ted-middle-east|ted/ted_mul_middle-east_2026-01.zim|TED — Middle East (talks on MENA region)|992 MB"
    "ted-music|ted/ted_mul_music_2026-01.zim|TED — Music (talks on performance, composition, cognition)|3.82 GB"
    "ted-philosophy|ted/ted_mul_philosophy_2026-01.zim|TED — Philosophy (talks on analytic, continental, applied traditions)|1.23 GB"
    "ted-ethics|ted/ted_mul_ethics_2026-01.zim|TED — Ethics (talks on applied and normative ethics)|921 MB"
    "ted-religion|ted/ted_mul_religion_2026-01.zim|TED — Religion (talks on comparative and lived religion)|1.58 GB"
    "ted-christianity|ted/ted_mul_christianity_2026-01.zim|TED — Christianity (talks on Christian thought, history, practice)|198 MB"
    "ted-islam|ted/ted_mul_islam_2026-01.zim|TED — Islam (talks on Islamic thought, history, practice)|189 MB"
    "ted-brain|ted/ted_mul_brain_2026-01.zim|TED — Brain (neuroscience, cognition, consciousness talks)|3.88 GB"
    "ted-economics|ted/ted_mul_economics_2026-01.zim|TED — Economics (markets, incentives, inequality, policy)|4.5 GB"
    "ted-behavioral-economics|ted/ted_mul_behavioral-economics_2026-01.zim|TED — Behavioural Economics (decision-making, psychology of choice)|424 MB"
    "ted-education|ted/ted_mul_education_2026-01.zim|TED — Education (learning, teaching, educational systems)|7.23 GB"
    "ted-environment|ted/ted_mul_environment_2026-01.zim|TED — Environment (climate, sustainability, conservation)|5.00 GB"
    "ted-government|ted/ted_mul_government_2026-02.zim|TED — Government (governance, policy, political institutions)|3.67 GB"
    "ted-international-development|ted/ted_mul_international-development_2026-01.zim|TED — International Development (poverty, aid, SDGs)|1.48 GB"
    "ted-politics|ted/ted_mul_politics_2026-02.zim|TED — Politics (ideologies, movements, political analysis)|4.87 GB"
    "ted-international-relations|ted/ted_mul_international-relations_2026-01.zim|TED — International Relations (diplomacy, conflict, global governance)|500 MB"
    "ted-public-health|ted/ted_mul_public-health_2026-01.zim|TED — Public Health (epidemiology, health policy, social determinants)|2.07 GB"
    "ted-sociology|ted/ted_mul_sociology_2026-02.zim|TED — Sociology (social behaviour, institutions, inequality)|469 MB"

    # Project Gutenberg (by Library of Congress category)
    "gutenberg|gutenberg/gutenberg_en_all_2025-11.zim|Project Gutenberg — all English books|206 GB"
    "gutenberg-fiction|gutenberg/gutenberg_en_lcc-pz_2026-03.zim|Gutenberg — Fiction & Juvenile|20 GB"
    "gutenberg-literature|gutenberg/gutenberg_en_lcc-ps_2026-03.zim|Gutenberg — American Literature|14 GB"
    "gutenberg-british-lit|gutenberg/gutenberg_en_lcc-pr_2026-03.zim|Gutenberg — English Literature|20 GB"
    "gutenberg-history|gutenberg/gutenberg_en_lcc-d_2026-03.zim|Gutenberg — World History|37 GB"
    "gutenberg-us-history|gutenberg/gutenberg_en_lcc-e_2026-03.zim|Gutenberg — US History|10 GB"
    "gutenberg-science|gutenberg/gutenberg_en_lcc-q_2026-03.zim|Gutenberg — Science|12 GB"
    # (LoC class B bundles philosophy, psychology and religion together.
    #  The label reflects all three so culture-and-personality /
    #  psychological-anthropology traditions surface for the right users.)
    # Project Gutenberg (by Library of Congress category)
    "gutenberg-philosophy|gutenberg/gutenberg_en_lcc-b_2026-03.zim|Gutenberg — Philosophy, Psychology & Religion|6 GB"
    "gutenberg-social-science|gutenberg/gutenberg_en_lcc-h_2026-03.zim|Gutenberg — Social Sciences|9.1 GB"
    "gutenberg-poetry|gutenberg/gutenberg_en_lcc-pq_2026-03.zim|Gutenberg — French/Italian/Spanish Lit|9.3 GB"
    "gutenberg-law|gutenberg/gutenberg_en_lcc-k_2026-03.zim|Gutenberg — Law|2.3 GB"
    "gutenberg-medicine|gutenberg/gutenberg_en_lcc-r_2026-03.zim|Gutenberg — Medicine|3.3 GB"
    "gutenberg-music|gutenberg/gutenberg_en_lcc-m_2026-03.zim|Gutenberg — Music|4.2 GB"
    "gutenberg-art|gutenberg/gutenberg_en_lcc-n_2026-03.zim|Gutenberg — Fine Arts|37 GB"
    "gutenberg-military|gutenberg/gutenberg_en_lcc-u_2026-03.zim|Gutenberg — Military Science|1.4 GB"
    # (LoC class G bundles geography, anthropology, ethnology, archaeology,
    #  folklore and recreation together. The label reflects that so users
    #  searching for anthropology find this package.)
    # Project Gutenberg (by Library of Congress category)
    "gutenberg-geography|gutenberg/gutenberg_en_lcc-g_2026-03.zim|Gutenberg — Geography, Anthropology & Recreation|8.1 GB"
    "gutenberg-technology|gutenberg/gutenberg_en_lcc-t_2026-03.zim|Gutenberg — Technology|6.3 GB"
    "gutenberg-education|gutenberg/gutenberg_en_lcc-l_2026-03.zim|Gutenberg — Education|3.3 GB"
    "gutenberg-political|gutenberg/gutenberg_en_lcc-j_2026-03.zim|Gutenberg — Political Science|4.1 GB"
    "gutenberg-c|gutenberg/gutenberg_en_lcc-c_2025-12.zim|Gutenberg — Auxiliary sciences of history (archaeology, epigraphy, numismatics)|1.2 GB"
    "gutenberg-pa|gutenberg/gutenberg_en_lcc-pa_2026-03.zim|Gutenberg — Greek and Latin language and literature|498 MB"
    "gutenberg-pc|gutenberg/gutenberg_en_lcc-pc_2026-03.zim|Gutenberg — Romance languages and philology|172 MB"
    "gutenberg-pl|gutenberg/gutenberg_en_lcc-pl_2026-03.zim|Gutenberg — Eastern Asian, African and Oceanian languages and literatures|102 MB"
    "gutenberg-pn|gutenberg/gutenberg_en_lcc-pn_2026-03.zim|Gutenberg — Literature (general), drama, criticism, journalism|3 GB"
    "gutenberg-pt|gutenberg/gutenberg_en_lcc-pt_2026-03.zim|Gutenberg — Germanic and Scandinavian literatures|724 MB"
)

# --- Bundles ---
print_bundles() {
    echo "Bundles (download multiple packages at once):"
    echo ""
    echo "  dev-essentials    Core developer docs (~50 MB)"
    echo "                    python, javascript, typescript, node, git, docker, bash"
    echo ""
    echo "  web-dev           Web development stack (~58 MB)"
    echo "                    javascript, typescript, react, nextjs, tailwindcss, css, html,"
    echo "                    svg, node, php, phpunit, mariadb"
    echo ""
    echo "  krull-stack       Everything this repo actually uses (~20 MB)"
    echo "                    docker, bash, python, fastapi, typescript, javascript, node,"
    echo "                    vite, nginx, html, css, svg, git"
    echo ""
    echo "  data-science      Data science & ML (~75 MB)"
    echo "                    python, numpy, pandas, scikit"
    echo ""
    echo "  sysadmin          System administration (~5.5 GB)"
    echo "                    archlinux, stackexchange-unix, stackexchange-serverfault"
    echo ""
    echo "  community         Developer Q&A (~5 GB)"
    echo "                    stackexchange-unix, stackexchange-codereview,"
    echo "                    stackexchange-security, stackexchange-softeng"
    echo ""
    echo "  survival-essentials Survival, navigation, medicine, self-sufficiency (~1.9 GB)"
    echo "                    post-disaster, field-medicine, military-medicine, water,"
    echo "                    food-preparation, stackexchange-outdoors, appropedia, wikivoyage"
    echo ""
    echo "  cooking-essentials  Cooking knowledge & recipes (~360 MB)"
    echo "                    food-preparation, stackexchange-cooking, public-domain-recipes"
    echo ""
    echo "  gutenberg-essentials  Classic literature & reference (~65 GB)"
    echo "                    fiction, american lit, english lit, poetry, philosophy"
    echo ""
    echo "  gutenberg-stem    Science, technology, medicine (~22 GB)"
    echo "                    science, technology, medicine"
    echo ""
    echo "  gutenberg-all-english  All 18 LoC categories as resumable pieces (~212 GB)"
    echo "                    Equivalent to the 206 GB gutenberg monolith but"
    echo "                    downloadable as 18 separate resumable files."
    echo ""
    echo "  oxford-anthropology  Cultural, archaeological, linguistic & psychological (~24 GB)"
    echo "                    gutenberg-geography (LoC G: anthropology/ethnology/folklore),"
    echo "                    gutenberg-social-science (Durkheim, Weber, functionalism),"
    echo "                    gutenberg-philosophy (philosophy, psychology, religion),"
    echo "                    ted-anthropology, ted-archaeology,"
    echo "                    stackexchange-linguistics, stackexchange-psychology."
    echo "                    For biological/physical anthropology install a Wikipedia package."
    echo ""
    echo "  world-religions   Popular religious texts, references and lectures (~9 GB)"
    echo "                    gutenberg-philosophy (LoC B: KJV Bible, Augustine, Aquinas,"
    echo "                    Zarathustra, theology classics),"
    echo "                    stackexchange-christianity, stackexchange-islam,"
    echo "                    stackexchange-judaism, stackexchange-hinduism,"
    echo "                    stackexchange-buddhism,"
    echo "                    ted-religion, ted-christianity, ted-islam."
    echo ""
}

get_bundle_keys() {
    case "$1" in
        dev-essentials)
            echo "devdocs-python devdocs-javascript devdocs-typescript devdocs-node devdocs-git devdocs-docker devdocs-bash"
            ;;
        web-dev)
            echo "devdocs-javascript devdocs-typescript devdocs-react devdocs-nextjs devdocs-tailwindcss devdocs-css devdocs-html devdocs-svg devdocs-node devdocs-php devdocs-phpunit devdocs-mariadb"
            ;;
        krull-stack)
            echo "devdocs-docker devdocs-bash devdocs-python devdocs-fastapi devdocs-typescript devdocs-javascript devdocs-node devdocs-vite devdocs-nginx devdocs-html devdocs-css devdocs-svg devdocs-git"
            ;;
        data-science)
            echo "devdocs-python devdocs-numpy devdocs-pandas devdocs-scikit"
            ;;
        sysadmin)
            echo "archlinux stackexchange-unix stackexchange-serverfault"
            ;;
        community)
            echo "stackexchange-unix stackexchange-codereview stackexchange-security stackexchange-softeng"
            ;;
        survival-essentials)
            echo "post-disaster field-medicine military-medicine water food-preparation stackexchange-outdoors appropedia wikivoyage"
            ;;
        cooking-essentials)
            echo "food-preparation stackexchange-cooking public-domain-recipes"
            ;;
        gutenberg-essentials)
            echo "gutenberg-fiction gutenberg-literature gutenberg-british-lit gutenberg-poetry gutenberg-philosophy"
            ;;
        gutenberg-stem)
            echo "gutenberg-science gutenberg-technology gutenberg-medicine"
            ;;
        gutenberg-all-english)
            echo "gutenberg-fiction gutenberg-literature gutenberg-british-lit gutenberg-history gutenberg-us-history gutenberg-science gutenberg-philosophy gutenberg-social-science gutenberg-poetry gutenberg-law gutenberg-medicine gutenberg-music gutenberg-art gutenberg-military gutenberg-geography gutenberg-technology gutenberg-education gutenberg-political"
            ;;
        # oxford-anthropology maps to the four-field model of anthropology:
        #   cultural/social → gutenberg-geography (LCC G: anthropology,
        #                     ethnology, folklore) + gutenberg-social-science
        #                     (LCC H: Durkheim/Weber/functionalism) + ted-anthropology
        #   archaeological  → ted-archaeology + archaeology material inside LCC G
        #   linguistic      → stackexchange-linguistics
        #   psychological   → gutenberg-philosophy (LCC B: philosophy, psychology,
        #                     religion) + stackexchange-psychology
        # Biological/physical anthropology is not covered — no dedicated ZIM
        # exists upstream; install a Wikipedia package for that subfield.
        oxford-anthropology)
            echo "gutenberg-geography gutenberg-social-science gutenberg-philosophy ted-anthropology ted-archaeology stackexchange-linguistics stackexchange-psychology"
            ;;
        # --- Oxford University graduate bundles (Humanities division) ---
        # Produced by the oxford-bundles skill's bodleian-librarian +
        # tutor-magister + catalog-archivist pipeline on 2026-04-08.
        # oxford-anthropology is the renamed `anthropology` bundle, kept
        # in the Oxford namespace for a consistent /library experience.
        # Subjects with no Kiwix coverage (Oncology, Paediatrics, Clinical
        # Psychiatry, Surgical Sciences, etc.) are in the MPLS/Medical divs.
        # NB: each case body is exactly one echo — no inline comments,
        # no blank lines. The catalog.ts parser regex is strict about this.
        oxford-archaeology)
            echo "ted-archaeology gutenberg-geography"
            ;;
        oxford-history-of-art)
            echo "gutenberg-art"
            ;;
        oxford-asian-middle-eastern-studies)
            echo "ted-asia ted-middle-east gutenberg-history gutenberg-pl"
            ;;
        oxford-classics)
            echo "gutenberg-pa stackexchange-latin gutenberg-history"
            ;;
        oxford-english-language-literature)
            echo "gutenberg-british-lit gutenberg-literature gutenberg-pn stackexchange-literature"
            ;;
        oxford-history)
            echo "gutenberg-history gutenberg-us-history gutenberg-c wikipedia-history stackexchange-history stackexchange-hsm"
            ;;
        oxford-law)
            echo "gutenberg-law stackexchange-law"
            ;;
        oxford-linguistics)
            echo "stackexchange-linguistics wiktionary stackexchange-latin gutenberg-pa"
            ;;
        oxford-medieval-modern-languages)
            echo "gutenberg-poetry gutenberg-pt gutenberg-pc"
            ;;
        oxford-music)
            echo "gutenberg-music mutopia-project open-music-theory stackexchange-music ted-music"
            ;;
        oxford-philosophy)
            echo "gutenberg-philosophy internet-encyclopedia-philosophy stackexchange-philosophy ted-philosophy ted-ethics"
            ;;
        oxford-theology-religion)
            echo "gutenberg-philosophy stackexchange-hinduism stackexchange-buddhism stackexchange-judaism ted-religion"
            ;;
        world-religions)
            echo "gutenberg-philosophy stackexchange-christianity stackexchange-islam stackexchange-judaism stackexchange-hinduism stackexchange-buddhism ted-religion ted-christianity ted-islam"
            ;;
        oxford-fine-art)
            echo "gutenberg-art"
            ;;
        # --- Oxford Mathematical, Physical and Life Sciences ---
        oxford-biology)
            echo "libretexts-biology stackexchange-biology wikipedia-molcell gutenberg-science"
            ;;
        oxford-chemistry)
            echo "libretexts-chemistry stackexchange-chemistry wikipedia-chemistry gutenberg-science"
            ;;
        oxford-computer-science)
            echo "algorithms-erickson stackexchange-cs stackexchange-cstheory stackoverflow"
            ;;
        oxford-earth-sciences)
            echo "stackexchange-earthscience gutenberg-geography gutenberg-science"
            ;;
        oxford-engineering)
            echo "libretexts-engineering stackexchange-engineering stackexchange-electronics gutenberg-technology"
            ;;
        oxford-materials)
            echo "stackexchange-mattermodeling gutenberg-technology gutenberg-science"
            ;;
        oxford-mathematics)
            echo "stackexchange-math libretexts-mathematics planetmath wikipedia-mathematics gutenberg-science"
            ;;
        oxford-physics)
            echo "libretexts-physics stackexchange-physics wikipedia-physics gutenberg-science"
            ;;
        oxford-statistics)
            echo "stackexchange-stats libretexts-statistics learningstats-r"
            ;;
        # --- Oxford Medical Sciences (Clinical Psychology has no Kiwix coverage) ---
        oxford-clinical-medicine)
            echo "libretexts-medicine stackexchange-medicalsciences surviving-residency wikem field-medicine military-medicine gutenberg-medicine"
            ;;
        oxford-clinical-neurosciences)
            echo "ted-brain stackexchange-psychology libretexts-medicine gutenberg-medicine"
            ;;
        oxford-medicine)
            echo "libretexts-medicine librepathology stackexchange-medicalsciences surviving-residency wikem gutenberg-medicine field-medicine military-medicine"
            ;;
        oxford-neuroscience)
            echo "ted-brain stackexchange-psychology libretexts-medicine"
            ;;
        # --- Oxford Social Sciences ---
        oxford-economics)
            echo "gutenberg-social-science stackexchange-economics ted-economics ted-behavioral-economics"
            ;;
        oxford-education)
            echo "gutenberg-education ted-education stackexchange-academia libretexts-k12"
            ;;
        oxford-geography-environment)
            echo "gutenberg-geography wikipedia-geography encyclopedia-environment ted-environment wikivoyage"
            ;;
        oxford-global-area-studies)
            echo "ted-asia ted-middle-east gutenberg-history gutenberg-pl"
            ;;
        oxford-government)
            echo "gutenberg-political ted-government stackexchange-politics"
            ;;
        oxford-international-development)
            echo "ted-international-development cd3wd gutenberg-social-science appropedia"
            ;;
        oxford-politics-international-relations)
            echo "gutenberg-political ted-politics ted-international-relations stackexchange-politics"
            ;;
        oxford-social-policy-intervention)
            echo "ted-public-health gutenberg-social-science appropedia"
            ;;
        oxford-sociology)
            echo "gutenberg-social-science ted-sociology wikipedia-sociology"
            ;;
        *)
            echo ""
            ;;
    esac
}

# --- Functions ---
print_usage() {
    echo "Usage: $0 <package|bundle> [package2 ...]"
    echo ""
    echo "Packages:"
    echo ""
    echo "  Developer Documentation:"
    for entry in "${CATALOG[@]}"; do
        IFS='|' read -r key file desc size <<< "$entry"
        case "$key" in devdocs-*)
            printf "    %-25s %s (%s)\n" "$key" "$desc" "$size"
            ;;
        esac
    done
    echo ""
    echo "  Stack Exchange:"
    for entry in "${CATALOG[@]}"; do
        IFS='|' read -r key file desc size <<< "$entry"
        case "$key" in stackexchange-*|stackoverflow)
            printf "    %-25s %s (%s)\n" "$key" "$desc" "$size"
            ;;
        esac
    done
    echo ""
    echo "  Survival & Self-Sufficiency:"
    for entry in "${CATALOG[@]}"; do
        IFS='|' read -r key file desc size <<< "$entry"
        case "$key" in post-disaster|field-medicine|military-medicine|water|appropedia|energypedia|wikivoyage|ifixit)
            printf "    %-25s %s (%s)\n" "$key" "$desc" "$size"
            ;;
        esac
    done
    echo ""
    echo "  Cooking:"
    for entry in "${CATALOG[@]}"; do
        IFS='|' read -r key file desc size <<< "$entry"
        case "$key" in food-preparation|foss-cooking|public-domain-recipes|grimgrains|based-cooking)
            printf "    %-25s %s (%s)\n" "$key" "$desc" "$size"
            ;;
        esac
    done
    echo ""
    echo "  Linux:"
    for entry in "${CATALOG[@]}"; do
        IFS='|' read -r key file desc size <<< "$entry"
        case "$key" in archlinux)
            printf "    %-25s %s (%s)\n" "$key" "$desc" "$size"
            ;;
        esac
    done
    echo ""
    echo "  Reference:"
    for entry in "${CATALOG[@]}"; do
        IFS='|' read -r key file desc size <<< "$entry"
        case "$key" in wiktionary)
            printf "    %-25s %s (%s)\n" "$key" "$desc" "$size"
            ;;
        esac
    done
    echo ""
    echo "  Anthropology & Humanities:"
    for entry in "${CATALOG[@]}"; do
        IFS='|' read -r key file desc size <<< "$entry"
        case "$key" in ted-*)
            printf "    %-25s %s (%s)\n" "$key" "$desc" "$size"
            ;;
        esac
    done
    echo ""
    echo "  Project Gutenberg (60,000+ free books):"
    for entry in "${CATALOG[@]}"; do
        IFS='|' read -r key file desc size <<< "$entry"
        case "$key" in gutenberg*)
            printf "    %-25s %s (%s)\n" "$key" "$desc" "$size"
            ;;
        esac
    done
    echo ""
    print_bundles
    echo "Examples:"
    echo "  $0 devdocs-python devdocs-git       # Download two packages"
    echo "  $0 dev-essentials                    # Download the developer essentials bundle"
    echo "  $0 archlinux stackexchange-unix      # Download Arch Wiki + Unix Q&A"
    echo ""
    echo "After downloading, restart Kiwix: docker restart krull-kiwix"
}

download_package() {
    local key="$1"
    local file=""
    local desc=""
    local size=""

    for entry in "${CATALOG[@]}"; do
        IFS='|' read -r k f d s <<< "$entry"
        if [ "$k" = "$key" ]; then
            file="$f"
            desc="$d"
            size="$s"
            break
        fi
    done

    if [ -z "$file" ]; then
        echo "[-] Unknown package: $key"
        return 1
    fi

    local filename
    filename=$(basename "$file")

    # Compare on-disk size to the catalog's expected size. Only
    # shortcircuit as "already downloaded" when the local file is at
    # least as large as expected — otherwise fall through so curl -C -
    # can resume a previous partial download. Without this check a
    # 20 GB file interrupted at 873 MB would be reported as "already
    # downloaded" and the bundle install would fail validation.
    if [ -f "$ZIM_DIR/$filename" ]; then
        local have_bytes want_bytes
        have_bytes=$(stat -c%s "$ZIM_DIR/$filename" 2>/dev/null || stat -f%z "$ZIM_DIR/$filename" 2>/dev/null || echo 0)
        want_bytes=$(dl_parse_size "$size")
        # Allow a ~5% slack because catalog sizes are human-rounded
        # ("20 GB") and the real file may be a bit smaller.
        if [ "$want_bytes" -gt 0 ] && [ "$have_bytes" -ge "$((want_bytes * 95 / 100))" ]; then
            echo "[+] Already downloaded: $desc ($filename)"
            return 0
        fi
        echo "[~] Resuming partial download: $desc ($(($have_bytes / 1024 / 1024)) MB of $size)"
    fi

    echo "[*] Downloading: $desc ($size)"
    echo "    File: $filename"

    # Declare this file in the progress manifest so the library page
    # can compute percent by stat-ing it. dl_run_curl wraps curl with
    # --fail and logs bad URLs to errors.jsonl on failure.
    dl_state_add "$ZIM_DIR/$filename" "$(dl_parse_size "$size")"
    if ! dl_run_curl "$ZIM_DIR/$filename" \
        "https://download.kiwix.org/zim/$file" \
        --progress-bar; then
        rm -f "$ZIM_DIR/$filename"
        echo "[-] Download failed: $filename (the catalog URL may be stale upstream)"
        return 1
    fi

    echo "[+] Done: $filename"
    echo ""
}

# --- Main ---
if [ $# -eq 0 ]; then
    print_usage
    exit 1
fi

PACKAGES=""

for arg in "$@"; do
    # Check if it's a bundle
    bundle_keys=$(get_bundle_keys "$arg")
    if [ -n "$bundle_keys" ]; then
        PACKAGES="$PACKAGES $bundle_keys"
    else
        PACKAGES="$PACKAGES $arg"
    fi
done

# Remove duplicates while preserving order
PACKAGES=$(echo "$PACKAGES" | tr ' ' '\n' | awk '!seen[$0]++' | tr '\n' ' ')

# Label the active entry with the first argument the user passed —
# that way a bundle install shows up as the bundle name instead of
# the first expanded member.
DL_LABEL="$1"
PKG_COUNT=$(echo "$PACKAGES" | wc -w | tr -d ' ')
dl_state_begin knowledge "$DL_LABEL" "$DL_LABEL ($PKG_COUNT package$([ "$PKG_COUNT" != "1" ] && echo s))"

# Ensure the state entry is always cleared on exit, even on Ctrl-C
# or set -e aborts. Trap reads $? so the terminal status mirrors the
# actual exit code.
_dl_on_exit() {
    local ec=$?
    if [ "$ec" -ne 0 ]; then
        dl_state_end failed || true
    else
        dl_state_end done || true
    fi
}
trap _dl_on_exit EXIT

FAIL=0
for pkg in $PACKAGES; do
    download_package "$pkg" || FAIL=1
done

if [ "$FAIL" -eq 0 ]; then
    echo ""
    echo "All downloads complete. Restart Kiwix to load them:"
    echo "  docker restart krull-kiwix"
else
    exit 1
fi
