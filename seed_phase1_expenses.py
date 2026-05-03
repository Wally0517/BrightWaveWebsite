#!/usr/bin/env python3
"""
Seed Brightwave Apartment Phase 1 construction expenses into the database.

Run on the VPS:
    cd /srv/brightwavehabitat/app
    python seed_phase1_expenses.py            # insert for real
    python seed_phase1_expenses.py --dry-run  # preview only, no changes
    python seed_phase1_expenses.py --reset    # wipe existing Phase 1 expenses first, then insert
"""

import sys
import os
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app, db, ProjectExpense, Property

PROPERTY_NAME = 'Brightwave Apartment Phase 1'
PAYEE          = 'Al-Ameen A.'
CEO            = 'CEO'

# (expense_date, category, item_name, payee_name, quantity, unit_cost, amount, notes, is_approved)
# category choices: materials | labour | transport | equipment | permits | land | other
EXPENSES = [

    # ══════════════════════════════════════════
    # LAND STAGE  (Aug – Oct 2024)
    # ══════════════════════════════════════════
    (date(2024,  8, 10), 'land',      'Land Purchase',                          PAYEE, None,  None,    720000, 'Land acquisition for Phase 1 project site', True),
    (date(2024,  8, 10), 'labour',    'Farmer Settlement',                      PAYEE, None,  None,     30000, 'Farmer compensation for site land', True),
    (date(2024,  8, 20), 'labour',    'Borehole Survey (Pre-Construction)',      PAYEE, None,  None,    100000, 'Pre-construction borehole survey', True),
    (date(2024,  8, 20), 'labour',    'Site Clearing',                           PAYEE, None,  None,    150000, 'Site clearing before construction began', True),
    (date(2024,  9,  1), 'other',     'Road Clearing',                           PAYEE, None,  None,    200000, 'Access road clearing to site', True),
    (date(2024,  9,  5), 'permits',   'Survey',                                  PAYEE, None,  None,    300000, 'Land survey and mapping', True),
    (date(2024,  9,  5), 'permits',   'Architect Fee',                           PAYEE, None,  None,    370000, 'Architectural design and drawings', True),
    (date(2024,  9, 20), 'transport', 'Shipping Document',                       PAYEE, None,  None,     78000, 'Shipping/documentation costs', True),
    (date(2024,  9, 20), 'materials', 'Fences — 3 Coaches',                      PAYEE,    3, 300000,  900000, 'Three coach sections of boundary fence', True),
    (date(2024, 10, 10), 'permits',   'Lawyer for Agreement',                    PAYEE, None,  None,    220000, 'Legal agreement for land purchase', True),
    (date(2024, 10, 10), 'permits',   'Certificate of Occupancy (C of O)',       PAYEE, None,  None,    712000, 'Government Certificate of Occupancy for the property', True),

    # ══════════════════════════════════════════
    # MAIN BUILDING MATERIALS  (Nov 2024 – Jan 2025)
    # ══════════════════════════════════════════
    (date(2024, 11,  5), 'materials', '9-Inch Blocks — Main (700 pcs)',          PAYEE,  700,  3500,   2450000, '9-inch blocks × 700 pieces at ₦3,500 each', True),
    (date(2024, 11,  5), 'materials', '9-Inch Blocks — Extra (600 pcs)',         PAYEE,  600,  1100,    660000, 'Extra 9-inch blocks × 600 pieces at ₦1,100 each', True),
    (date(2024, 11,  5), 'materials', 'Granite (30 tons)',                       PAYEE,   30, 20000,   600000, '30 tons of granite at ₦200,000 per 10 tons', True),
    (date(2024, 11, 10), 'materials', 'Shocking Sand (30 tons)',                 PAYEE,   30,  5500,   165000, '30 tons of shocking sand at ₦55,000 per 10 tons', True),
    (date(2024, 12,  5), 'materials', 'Sharp Sand (30 tons)',                    PAYEE,   30,  6500,   195000, '30 tons of sharp sand at ₦65,000 per ton', True),
    (date(2024, 12,  5), 'materials', 'Decking Sand (40 tons)',                  PAYEE,   40,  2000,    80000, '40 tons of decking sand', True),
    (date(2024, 12, 10), 'materials', 'Cement — 250 Bags (₦8,600/bag)',          PAYEE,  250,  8600,  2150000, '250 bags of cement at ₦8,600 per bag', True),
    (date(2025,  1,  8), 'materials', '6-Inch Blocks — Main (600 pcs)',          PAYEE,  600,  3500,  2100000, '6-inch blocks × 600 pieces at ₦3,500 each', True),
    (date(2025,  1,  8), 'materials', '6-Inch Blocks — Extra (450 pcs)',         PAYEE,  450,  1200,   540000, 'Extra 6-inch blocks × 450 pieces at ₦1,200 each', True),

    # ══════════════════════════════════════════
    # BRICKLAYER LABOUR
    # ══════════════════════════════════════════
    (date(2024, 11, 15), 'labour',    'Bricklayer — Foundation to Roofing',     PAYEE, None,  None,   900000, 'Full bricklayer payment from foundation up to roofing stage', True),
    (date(2025,  1, 15), 'labour',    'Bricklayer — Second Labor Payment',      PAYEE, None,  None,   300000, 'Second bricklayer labor installment', True),
    (date(2025,  2, 20), 'labour',    'Bricklayer — Pillars and Fencing',       PAYEE, None,  None,   300000, 'Bricklayer labor for pillars and fencing work', True),
    (date(2025,  3, 25), 'labour',    'Bricklayer — 3rd Stage',                 PAYEE, None,  None,   150000, 'Third stage bricklayer labor payment', True),
    (date(2026,  4, 15), 'labour',    'Bricklayer — Finishing Stage',           PAYEE, None,  None,   150000, 'Final finishing stage bricklayer labor', False),  # PENDING

    # ══════════════════════════════════════════
    # BOREHOLE / WATER SYSTEM  (Jan – Feb 2025)
    # ══════════════════════════════════════════
    (date(2025,  1, 20), 'labour',    'Borehole Digging — 180m',                PAYEE,  180, 14000,  2520000, 'Borehole drilling at ₦14,000/m × 180m depth', True),
    (date(2025,  1, 20), 'materials', 'Borehole Pipe',                           PAYEE, None,  None,   180000, 'Pipes for borehole installation', True),
    (date(2025,  1, 20), 'labour',    'Electrician — Borehole Setup',            PAYEE, None,  None,    20000, 'Electrical work for borehole pump connection', True),
    (date(2025,  2,  8), 'equipment', 'Pumping Machine',                         PAYEE, None,  None,   200000, 'Water pumping machine for borehole', True),
    (date(2025,  2,  8), 'materials', 'Electrical Wire (Borehole)',               PAYEE, None,  None,    34600, 'Wire for borehole pumping machine', True),
    (date(2025,  2,  8), 'labour',    'Plumber Labor — Water System',             PAYEE, None,  None,    30000, 'Plumber labor for water distribution setup', True),
    (date(2025,  2,  8), 'equipment', '2000L Storex Water Tank',                  PAYEE, None,  None,   300000, '2000-litre Storex overhead water tank', True),

    # ══════════════════════════════════════════
    # ELECTRICIAN — FIRST STAGE  (Mar 2025)
    # ══════════════════════════════════════════
    (date(2025,  3, 10), 'materials', 'Electrical Materials — First Stage',      PAYEE, None,  None,   640000, 'Conduit boxes, junction boxes, PVC trunking, earth wire, warning tape, cable clips, saddles, flexible conduit, PVC pipes (total excl. labor)', True),
    (date(2025,  3, 10), 'labour',    'Electrician Labor — First Stage',          PAYEE, None,  None,   150000, 'First stage electrical installation labor', True),

    # ══════════════════════════════════════════
    # PLUMBING — FIRST STAGE  (Mar 2025)
    # ══════════════════════════════════════════
    (date(2025,  3, 15), 'materials', 'Plumbing Materials — First Stage',        PAYEE, None,  None,   574000, '½" / ¾" / 2" / 3" / 4" Kano PVC pipes, elbows, tees, adapters, stop corks, bends, ballow wire, thread tape, gum (see notes for full breakdown)', True),
    (date(2025,  3, 15), 'labour',    'Plumber Labor — First Stage',              PAYEE, None,  None,   150000, 'First stage plumbing installation labor', True),

    # ══════════════════════════════════════════
    # ROOFING WORK  (Apr – Jun 2025)
    # ══════════════════════════════════════════
    (date(2025,  4,  5), 'materials', 'Aluminium Roof — First Half',             PAYEE, None,  None,  2500000, 'First 50% of aluminium roofing sheets', True),
    (date(2025,  4,  5), 'materials', 'Iron / Ring / Banding Wire',              PAYEE, None,  None,   900000, 'Iron, ring and banding wire for roofing structure', True),
    (date(2025,  4, 15), 'materials', 'Aluminium Roof — Second Half',            PAYEE, None,  None,  2500000, 'Second 50% of aluminium roofing sheets', True),
    (date(2025,  4, 15), 'materials', 'Parapet',                                 PAYEE, None,  None,  1400000, 'Parapet wall construction', True),
    (date(2025,  4, 15), 'materials', 'Roofing Planks',                          PAYEE, None,  None,  3150000, 'Timber planks for roofing frame structure', True),
    (date(2025,  4, 25), 'labour',    'Carpenter Labor — Roofing',               PAYEE, None,  None,   350000, 'Carpenter labor for full roofing work', True),
    (date(2025,  4, 25), 'labour',    'Carpenter Help — Roofing Assistant',      PAYEE, None,  None,   100000, 'Additional roofing assistant carpenter labor', True),
    (date(2025,  5,  5), 'materials', 'Planks (Casting)',                        PAYEE, None,  None,   150000, 'Planks for casting work', True),
    (date(2025,  5,  5), 'materials', 'Cement — 12 Bags (Roofing Stage)',        PAYEE,   12,  9833,   118000, '12 bags of cement at roofing stage', True),
    (date(2025,  5, 15), 'materials', 'Cement — 60 Bags (Roofing Stage)',        PAYEE,   60, 10500,   630000, '60 bags of cement for roofing completion', True),
    (date(2025,  5, 15), 'transport', 'Transport — Roofing Materials',           PAYEE, None,  None,    20000, 'Delivery transport for roofing materials', True),
    (date(2025,  6,  5), 'materials', 'Water Direction / Drainage',              PAYEE, None,  None,   250000, 'Water direction and drainage channel installation', True),

    # ══════════════════════════════════════════
    # SOAKWAY & PILLAR  (Jun 2025)
    # ══════════════════════════════════════════
    (date(2025,  6, 10), 'materials', 'Soakway and Pillar Construction',         PAYEE, None,  None,   950000, 'Full soakway and pillar: moulding, labor, irons, blocks, granite, iron for pillar, shocking sand, cement', True),

    # ══════════════════════════════════════════
    # ROOMS FILLINGS  (Jun 2025)
    # ══════════════════════════════════════════
    (date(2025,  6, 20), 'materials', 'German Floor — Sand Filling',             PAYEE, None,  None,  1200000, 'German floor sand filling for all rooms', True),
    (date(2025,  6, 20), 'materials', 'Bundle of Nylon (DPC)',                   PAYEE, None,  None,   135000, 'Damp-proof course nylon sheets for floor filling', True),
    (date(2025,  6, 20), 'labour',    'Labor — Floor Filling',                   PAYEE, None,  None,    20000, 'Labor for rooms floor filling work', True),

    # ══════════════════════════════════════════
    # SECURITY CAMERA & SECURITY POST  (Jul – Aug 2025)
    # ══════════════════════════════════════════
    (date(2025,  7,  5), 'materials', 'Security Post — Pillars',                 PAYEE, None,  None,   100000, 'Pillars for security post structure', True),
    (date(2025,  7,  5), 'materials', 'Security Post — Irons',                   PAYEE, None,  None,   260000, 'Iron rods for security post', True),
    (date(2025,  7,  5), 'materials', 'Security Post — Cement for Decking',      PAYEE, None,  None,   294000, 'Cement for security post decking slab', True),
    (date(2025,  7,  5), 'materials', 'Security Post — Rods',                    PAYEE, None,  None,   100000, 'Reinforcement rods for security post', True),
    (date(2025,  8,  5), 'equipment', 'Camera Pole',                              PAYEE, None,  None,    90000, 'CCTV camera mounting pole', True),
    (date(2025,  8,  5), 'equipment', 'Security Cameras (CCTV)',                  PAYEE, None,  None,   219500, 'CCTV security camera system installation', True),
    (date(2025,  8,  5), 'materials', 'Security Post — Blocks',                   PAYEE, None,  None,   250000, 'Blocks for security post construction', True),
    (date(2025,  8,  5), 'materials', 'Security Parapets',                        PAYEE, None,  None,   400000, 'Parapet walls around security area', True),

    # ══════════════════════════════════════════
    # FENCE WORK DESIGN  (Aug 2025)
    # ══════════════════════════════════════════
    (date(2025,  8, 15), 'materials', 'Fence Blocks — 1100 Pieces',              PAYEE, 1100,   650,   715000, '1100 blocks for fence design work', True),
    (date(2025,  8, 15), 'materials', 'Fence Iron',                               PAYEE, None,  None,   225000, 'Iron for fence design and reinforcement', True),
    (date(2025,  8, 15), 'materials', 'Fence Cement and Designs',                 PAYEE, None,  None,   685000, 'Cement plus decorative designs for fence (balance of ₦1,625,000 fence total)', True),

    # ══════════════════════════════════════════
    # CARPENTER LABOUR — MISC
    # ══════════════════════════════════════════
    (date(2025,  7, 25), 'labour',    'Carpenter — All Other Works',             PAYEE, None,  None,   200000, 'General carpentry work (all other works, estimate)', True),
    (date(2025,  8, 10), 'labour',    'Carpenter — Security Post Help',          PAYEE, None,  None,    50000, 'Carpenter assistance at security post construction', True),

    # ══════════════════════════════════════════
    # THIRD STAGE MATERIALS  (Jul – Sep 2025)
    # ══════════════════════════════════════════
    (date(2025,  7, 15), 'materials', 'Window Pipes',                            PAYEE, None,  None,   300000, 'Pipes for window frame installation', True),
    (date(2025,  7, 15), 'materials', 'Kitchen Wastage Pipes',                   PAYEE, None,  None,   200000, 'Kitchen waste pipe system', True),
    (date(2025,  7, 20), 'materials', 'Doors',                                   PAYEE, None,  None,   940000, 'Interior and exterior doors for all rooms', True),
    (date(2025,  8, 20), 'materials', 'Cement — 40 Bags (3rd Stage)',            PAYEE,   40,  9500,   380000, '40 bags of cement for third stage work', True),
    (date(2025,  8, 20), 'materials', 'Gate',                                    PAYEE, None,  None,   420000, 'Main entrance gate fabrication and installation', True),
    (date(2025,  8, 20), 'materials', 'Gate Oil and Hardware',                   PAYEE, None,  None,    50000, 'Gate oil, hinges and hardware fittings', True),
    (date(2025,  8, 20), 'labour',    'Welder Labor — Gate and Fence',           PAYEE, None,  None,    50000, 'Welder labor for gate and fence ironwork', True),
    (date(2025,  9,  5), 'materials', 'Hidden Roof Block',                       PAYEE, None,  None,   800000, 'Hidden roof block construction materials', True),
    (date(2025,  9,  5), 'materials', 'Compound Filling — 3rd Stage',            PAYEE, None,  None,   300000, 'Compound area filling materials (3rd stage)', True),
    (date(2025,  9,  5), 'materials', 'Windows Frames',                          PAYEE, None,  None,   700000, 'Window frames for all openings', True),
    (date(2025,  9, 10), 'materials', 'Sands — 3rd Stage',                       PAYEE, None,  None,  1200000, 'Sand for third stage construction work', True),
    (date(2025,  9, 10), 'materials', 'Barb Wire',                               PAYEE, None,  None,   400000, 'Barbed wire for perimeter security', True),

    # ══════════════════════════════════════════
    # PLASTERING  (Oct 2025)
    # ══════════════════════════════════════════
    (date(2025, 10,  5), 'materials', 'Cement — Plastering',                     PAYEE, None,  None,  1450000, 'Cement for full building internal and external plastering', True),
    (date(2025, 10,  5), 'materials', 'Fence Cement — Plastering',               PAYEE, None,  None,   420000, 'Cement for fence plastering finish', True),
    (date(2025, 10,  5), 'labour',    'Welder — Fence Iron Installation',         PAYEE, None,  None,   225000, 'Welder labor for fence iron installation during plastering', True),
    (date(2025, 10,  5), 'materials', 'Watery Cement — 30 Bags',                 PAYEE,   30, 23333,   700000, '30 bags of watery cement for surface finish', True),

    # ══════════════════════════════════════════
    # FINISHING LEVEL  (Nov 2025 – Apr 2026)
    # ══════════════════════════════════════════
    (date(2025, 11,  5), 'materials', 'Wardrobes and Kitchenette Cupboards',     PAYEE, None,  None,  2300000, 'Built-in wardrobes for 5 bedrooms (₦1,150,000) + kitchenette cupboards', True),
    (date(2025, 11, 15), 'materials', 'Room Tiling — Complete',                  PAYEE, None,  None,  4267100, 'Full tiling: 5-room tiles ₦1,263,550 + cement 100 bags ₦1,000,000 + 2 tons sharp sand + workmanship ₦450,000', True),
    (date(2025, 12,  5), 'materials', '11 Bathroom Doors',                       PAYEE,   11, 86364,   950000, '11 bathroom doors supplied and fitted', True),
    (date(2025, 12, 15), 'labour',    'Electrician Wiring — Complete',           PAYEE, None,  None,  3202000, 'Full building electrical wiring completed: 5 rooms ₦1,452,000 + all common areas and service wiring', True),
    (date(2026,  1, 10), 'materials', 'Snooker Shade / Canopy',                  PAYEE, None,  None,   750000, 'Snooker shade/canopy structure', True),
    (date(2026,  1, 20), 'materials', 'Compound Final — Sand, Filling, Nylon, Cement', PAYEE, None, None, 1500000, 'Final compound: sand + filling + nylon ₦745,000 + 80 bags cement ₦800,000', True),
    (date(2026,  2, 10), 'materials', 'Window Glass',                            PAYEE, None,  None,  1200000, 'Window glass for all openings', True),
    (date(2026,  2, 20), 'materials', 'POP Installation and Materials',          PAYEE, None,  None,  1200000, 'Plaster of Paris (POP) installation plus all materials', True),
    (date(2026,  3, 10), 'materials', 'POP Cement',                              PAYEE, None,  None,  1250000, 'Cement for POP plastering finish', True),

    # ══════════════════════════════════════════
    # STILL FINISHING — BATHROOM FITTINGS
    # ══════════════════════════════════════════
    (date(2026,  3, 20), 'equipment', 'Bathroom Fittings Deposit (Mirror, Shower, WC, Bases, Tank)', PAYEE, None, None, 575000, 'Deposit paid for bathroom fittings: mirror, shower, washing base, kitchen washing base, WC, second tank. Full total ₦1,150,000', True),

    # ══════════════════════════════════════════
    # CARPENTER LABOUR — FINISHING
    # ══════════════════════════════════════════
    (date(2026,  4, 10), 'labour',    'Carpenter — Finishing Help',              PAYEE, None,  None,    50000, 'Carpentry assistance at final finishing stage', True),

    # ══════════════════════════════════════════
    # MISCELLANEOUS
    # ══════════════════════════════════════════
    (date(2025,  3,  1), 'other',     'Fuel Costs (Q1 2025)',                    PAYEE, None,  None,   200000, 'Fuel for generator and site vehicles — Q1 2025', True),
    (date(2025,  9,  1), 'other',     'Fuel Costs (Q3 2025)',                    PAYEE, None,  None,   200000, 'Fuel for generator and site vehicles — Q3 2025', True),
    (date(2025,  1,  5), 'equipment', 'Generator Repair',                        PAYEE, None,  None,    50000, 'Generator repair and fix', True),
    (date(2025,  6, 15), 'equipment', 'Generator Maintenance',                   PAYEE, None,  None,   100000, 'Generator maintenance and servicing', True),
    (date(2025,  5, 20), 'other',     'Impromptu / Miscellaneous Expenses',      PAYEE, None,  None,   450000, 'Impromptu and miscellaneous expenses during construction', True),
    (date(2025,  4, 10), 'transport', 'Transport Costs (Apr–Aug 2025)',           PAYEE, None,  None,   150000, 'Transport for materials and workers — April to August 2025', True),
    (date(2025,  9, 15), 'transport', 'Transport Costs (Sep 2025 – Present)',    PAYEE, None,  None,   150000, 'Transport for materials and workers — September 2025 onwards', True),

    # ══════════════════════════════════════════
    # MANAGER PAYMENT
    # ══════════════════════════════════════════
    (date(2026,  4, 25), 'other',     'Manager Payment — BJ (Kwara)',            'BJ (Kwara Manager)', None, None, 300000, 'Payment to Kwara State project manager (BJ)', True),
]


def total_naira(approved_only=False):
    total = 0
    for exp in EXPENSES:
        is_approved = exp[8]
        if approved_only and not is_approved:
            continue
        total += exp[6]
    return total


def main():
    dry_run = '--dry-run' in sys.argv
    reset   = '--reset'   in sys.argv

    print(f"{'[DRY RUN] ' if dry_run else ''}BrightWave Phase 1 Expense Seeder")
    print(f"Total expenses to insert : {len(EXPENSES)}")
    print(f"Grand total (all)        : ₦{total_naira():>15,.0f}")
    print(f"Grand total (approved)   : ₦{total_naira(approved_only=True):>15,.0f}")
    print()

    with app.app_context():
        # Locate Phase 1 property
        from sqlalchemy import or_
        prop = Property.query.filter(
            or_(
                Property.title.ilike('%phase%1%'),
                Property.title.ilike('%phase1%'),
            )
        ).first()

        if not prop:
            print("ERROR: No Phase 1 property found in the database.")
            print("Please create it in the management portal first, then re-run.")
            sys.exit(1)

        print(f"Found property : '{prop.title}'  (ID {prop.id})")

        if not dry_run:
            old_title = prop.title
            prop.title = PROPERTY_NAME
            db.session.flush()
            print(f"Renamed        : '{old_title}'  →  '{PROPERTY_NAME}'")

        existing = ProjectExpense.query.filter_by(property_id=prop.id).count()
        print(f"Existing expenses in DB : {existing}")

        if existing > 0:
            if reset and not dry_run:
                ProjectExpense.query.filter_by(property_id=prop.id).delete()
                db.session.flush()
                print(f"Deleted {existing} existing expenses (--reset flag used)")
            elif not dry_run:
                ans = input(f"\n{existing} expenses already exist. Continue and ADD more? (y/N): ").strip().lower()
                if ans != 'y':
                    print("Aborted — no changes made.")
                    db.session.rollback()
                    return

        print()
        added = 0
        for exp_date, cat, name, payee, qty, unit_cost, amount, notes, approved in EXPENSES:
            status = 'approved' if approved else 'pending'
            if dry_run:
                flag = '' if approved else '  [PENDING]'
                print(f"  {exp_date}  [{cat:<10}]  ₦{amount:>12,.0f}  {name}{flag}")
            else:
                e = ProjectExpense(
                    property_id    = prop.id,
                    expense_date   = exp_date,
                    category       = cat,
                    item_name      = name,
                    payee_name     = payee,
                    quantity       = qty,
                    unit_cost      = unit_cost,
                    amount         = float(amount),
                    notes          = notes,
                    approval_status= status,
                    recorded_by    = CEO,
                )
                db.session.add(e)
            added += 1

        if dry_run:
            print(f"\n[DRY RUN] Would insert {added} expenses. No changes written.")
        else:
            db.session.commit()
            print(f"Done — inserted {added} expenses for '{PROPERTY_NAME}'.")


if __name__ == '__main__':
    main()
