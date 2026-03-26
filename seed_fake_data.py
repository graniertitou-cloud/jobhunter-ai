"""Seed the database with ~150 fake students and realistic activity."""
import random
from datetime import datetime, timedelta
from main import (
    SessionLocal, User, Student, Application, Message, SavedJob, Job,
    hash_password, Base, engine
)

random.seed(42)

FIRST_NAMES = [
    "Emma", "Louise", "Alice", "Chloe", "Lea", "Manon", "Camille", "Jade",
    "Lina", "Sarah", "Juliette", "Clara", "Margaux", "Ines", "Anna",
    "Marie", "Lucie", "Charlotte", "Zoe", "Eva", "Romane", "Agathe",
    "Mathilde", "Victoria", "Elsa", "Noemie", "Hugo", "Lucas", "Gabriel",
    "Louis", "Raphael", "Arthur", "Jules", "Adam", "Mael", "Leo",
    "Nathan", "Paul", "Tom", "Ethan", "Theo", "Maxime", "Alexandre",
    "Antoine", "Valentin", "Baptiste", "Clement", "Oscar", "Samuel",
    "Axel", "Romain", "Victor", "Simon", "Adrien", "Bastien", "Florian",
    "Dylan", "Tristan", "Nolan", "Quentin", "Damien", "Eliot", "Gabin",
    "Liam", "Mathis", "Robin", "Martin", "Enzo", "Noah", "Eliott",
    "Margot", "Pauline", "Oceane", "Anais", "Apolline", "Capucine",
    "Celeste", "Diane", "Elena", "Faustine", "Gabrielle", "Helene",
    "Iris", "Jeanne", "Lola", "Madeleine", "Nina", "Olivia", "Penelope",
    "Rose", "Sophie", "Victoire", "Yasmine", "Ambre", "Blanche",
    "Constance", "Doriane", "Emilie", "Flora", "Garance",
]

LAST_NAMES = [
    "Martin", "Bernard", "Dubois", "Thomas", "Robert", "Richard", "Petit",
    "Durand", "Leroy", "Moreau", "Simon", "Laurent", "Lefebvre", "Michel",
    "Garcia", "David", "Bertrand", "Roux", "Vincent", "Fournier",
    "Morel", "Girard", "Andre", "Mercier", "Dupont", "Lambert", "Bonnet",
    "Francois", "Martinez", "Legrand", "Garnier", "Faure", "Rousseau",
    "Blanc", "Guerin", "Muller", "Henry", "Roussel", "Nicolas", "Perrin",
    "Morin", "Mathieu", "Clement", "Gauthier", "Dumont", "Lopez",
    "Fontaine", "Chevalier", "Robin", "Masson", "Sanchez", "Noel",
    "Dufour", "Blanchard", "Brunet", "Giraud", "Riviere", "Arnaud",
    "Collet", "Lemoine", "Marchand", "Picard", "Renard", "Barbier",
]

SECTORS = [
    "Arts visuels & Mediation", "Musique & Spectacle vivant",
    "Cinema & Audiovisuel", "Mode & Luxe", "Communication & Digital",
    "Marche de l'art", "Patrimoine & Museologie",
]

PROMOS = ["Bachelor 1", "Bachelor 2", "Bachelor 3", "MBA 1", "MBA 2"]

COMPANIES_CULTURE = [
    "Musee du Louvre", "Centre Pompidou", "Palais de Tokyo",
    "Fondation Louis Vuitton", "Musee d'Orsay", "Grand Palais",
    "Galerie Perrotin", "Christie's Paris", "Sotheby's France",
    "Artcurial", "LVMH", "Kering", "Chanel", "Dior Couture",
    "Hermes International", "Balenciaga", "Saint Laurent Paris",
    "Canal+", "France Televisions", "Arte", "Gaumont", "Pathe",
    "UGC", "MK2", "Festival de Cannes", "Festival d'Avignon",
    "Philharmonie de Paris", "Opera de Paris", "Theatre du Chatelet",
    "AccorArena", "Olympia", "Live Nation France", "AEG Presents",
    "Publicis", "Havas", "BETC", "DDB Paris", "TBWA Paris",
    "Galeries Lafayette", "Le Bon Marche", "Drouot", "Piasa",
    "Maison de ventes Cornette de Saint Cyr", "Musee Picasso",
    "Jeu de Paume", "Cite de la Musique", "La Villette",
    "Musee du quai Branly", "Petit Palais",
]

JOB_TITLES = [
    "Assistant curateur", "Charge de production culturelle",
    "Assistant communication musee", "Coordinateur evenementiel",
    "Assistant galerie d'art", "Charge de mediation culturelle",
    "Assistant marketing luxe", "Coordinateur artistique",
    "Assistant production audiovisuelle", "Charge de relations presse",
    "Community manager culture", "Assistant commissaire d'exposition",
    "Charge de programmation", "Assistant direction artistique",
    "Coordinateur projets culturels", "Assistant patrimoine",
    "Charge de mecenat", "Assistant conservation",
    "Coordinateur festival", "Assistant edition d'art",
]

STATUSES_APP = ["a_envoyer", "envoyee", "relance", "entretien", "refusee", "obtenu"]
STAGE_STATUSES = ["searching", "found"]

MSG_FROM_ADMIN = [
    "Bonjour, comment avancent vos recherches de stage ?",
    "N'hesitez pas a postuler sur les offres que je vous ai envoyees.",
    "Je vous recommande de contacter cette entreprise directement.",
    "Votre CV est bien recu, je le transmets a mon reseau.",
    "Avez-vous eu des retours suite a vos candidatures ?",
    "Une nouvelle offre correspond a votre profil, regardez vos messages.",
    "Pensez a relancer les entreprises ou vous avez postule.",
    "Bravo pour votre entretien ! Tenez-moi au courant.",
    "Je vous ai trouve une opportunite interessante.",
    "Rappelez-vous de mettre a jour votre statut quand vous trouvez.",
]

MSG_FROM_STUDENT = [
    "Merci pour l'offre, je vais postuler !",
    "J'ai envoye ma candidature ce matin.",
    "J'ai un entretien prevu la semaine prochaine !",
    "Malheureusement je n'ai pas ete retenu(e).",
    "J'ai trouve mon stage, merci pour votre aide !",
    "Je cherche encore, mais j'ai quelques pistes.",
    "Pouvez-vous me recommander d'autres entreprises ?",
    "Mon CV est a jour, vous pouvez le consulter.",
    "J'ai relance l'entreprise comme vous me l'avez conseille.",
    "Merci beaucoup pour votre accompagnement !",
]


def seed():
    db = SessionLocal()
    try:
        # Check if already seeded
        existing_students = db.query(Student).count()
        if existing_students > 10:
            print(f"Already {existing_students} students, skipping seed.")
            return

        # Get admin user
        admin = db.query(User).filter(User.role == "admin").first()
        if not admin:
            print("No admin user found!")
            return

        # Get schools
        from main import School
        schools = db.query(School).all()
        school_ids = [s.id for s in schools]
        if not school_ids:
            print("No schools found!")
            return

        print(f"Seeding with {len(school_ids)} schools: {[s.name for s in schools]}")

        now = datetime.utcnow()
        student_id_counter = 0
        all_student_ids = []
        all_student_user_ids = []

        # Create fake jobs in DB for saved jobs
        job_ids = []
        for i in range(40):
            job = Job(
                title=random.choice(JOB_TITLES),
                company=random.choice(COMPANIES_CULTURE),
                location=random.choice(["Paris", "Paris 8e", "Paris 3e", "Bordeaux", "Lyon", "Neuilly-sur-Seine"]),
                url=f"https://example.com/job/{i+1}",
                platform=random.choice(["LinkedIn", "WTTJ", "France Travail", "Profilculture"]),
                sector=random.choice(SECTORS),
                description=f"Stage passionnant dans le secteur culturel. Missions variees en lien avec {random.choice(SECTORS).lower()}.",
                contract_type=random.choice(["Stage", "Alternance"]),
                score=round(random.uniform(60, 95), 1),
                scraped_at=now - timedelta(days=random.randint(0, 30)),
            )
            db.add(job)
            db.flush()
            job_ids.append(job.id)

        used_emails = set()
        used_names = set()

        for school_id in school_ids:
            num_students = random.randint(45, 55)
            for _ in range(num_students):
                # Generate unique name
                while True:
                    fn = random.choice(FIRST_NAMES)
                    ln = random.choice(LAST_NAMES)
                    if (fn, ln) not in used_names:
                        used_names.add((fn, ln))
                        break

                email = f"{fn.lower()}.{ln.lower()}@icart.fr"
                suffix = 1
                while email in used_emails:
                    email = f"{fn.lower()}.{ln.lower()}{suffix}@icart.fr"
                    suffix += 1
                used_emails.add(email)

                # Decide status: ~30% found, ~70% searching
                is_found = random.random() < 0.30
                stage_status = "found" if is_found else "searching"
                stage_company = random.choice(COMPANIES_CULTURE) if is_found else ""
                domain = random.choice(SECTORS) if is_found else ""

                # Create user
                user = User(
                    email=email,
                    password_hash=hash_password("icart2025"),
                    role="student",
                    first_name=fn,
                    last_name=ln,
                )
                db.add(user)
                db.flush()

                # Create student profile
                last_activity = now - timedelta(
                    hours=random.randint(0, 72),
                    minutes=random.randint(0, 59),
                )
                student = Student(
                    user_id=user.id,
                    first_name=fn,
                    last_name=ln,
                    promo=random.choice(PROMOS),
                    school_id=school_id,
                    target_sector=random.choice(SECTORS),
                    stage_status=stage_status,
                    stage_company=stage_company,
                    domain_found=domain,
                    last_activity_at=last_activity,
                    notes=random.choice([
                        "", "", "",  # Most have no notes
                        "Tres motive, en contact avec plusieurs galeries.",
                        "A besoin d'aide pour son CV.",
                        "Recherche idealement dans le 8e arrondissement.",
                        "Bilingue anglais, profil international.",
                        "Interesse par le marche de l'art asiatique.",
                    ]),
                )
                db.add(student)
                db.flush()

                all_student_ids.append(student.id)
                all_student_user_ids.append(user.id)

                # Create applications (2-6 per student)
                num_apps = random.randint(1, 6)
                for a in range(num_apps):
                    if is_found and a == 0:
                        app_status = "obtenu"
                    else:
                        app_status = random.choice(STATUSES_APP)

                    applied_date = now - timedelta(days=random.randint(0, 45))
                    app = Application(
                        student_id=student.id,
                        job_title=random.choice(JOB_TITLES),
                        company=random.choice(COMPANIES_CULTURE),
                        url=f"https://example.com/apply/{random.randint(1000, 9999)}",
                        status=app_status,
                        notes=random.choice([
                            "", "", "Candidature spontanee",
                            "Via le reseau de l'ecole",
                            "Recommande par un ancien",
                        ]),
                        applied_at=applied_date,
                        updated_at=applied_date + timedelta(days=random.randint(0, 10)),
                    )
                    db.add(app)

                # Save some jobs (0-5 per student)
                num_saved = random.randint(0, 5)
                saved_job_ids = random.sample(job_ids, min(num_saved, len(job_ids)))
                for jid in saved_job_ids:
                    sj = SavedJob(
                        student_id=student.id,
                        job_id=jid,
                        saved_at=now - timedelta(days=random.randint(0, 20)),
                    )
                    db.add(sj)

                # Create messages (0-8 exchanges per student)
                num_msgs = random.randint(0, 8)
                for m in range(num_msgs):
                    msg_time = now - timedelta(
                        days=random.randint(0, 30),
                        hours=random.randint(0, 23),
                        minutes=random.randint(0, 59),
                    )
                    if random.random() < 0.5:
                        # Message from admin
                        msg = Message(
                            from_user_id=admin.id,
                            to_student_id=student.id,
                            content=random.choice(MSG_FROM_ADMIN),
                            sent_at=msg_time,
                            read=1 if random.random() < 0.7 else 0,
                        )
                    else:
                        # Message from student
                        msg = Message(
                            from_user_id=user.id,
                            to_student_id=student.id,
                            content=random.choice(MSG_FROM_STUDENT),
                            sent_at=msg_time,
                            read=1 if random.random() < 0.8 else 0,
                        )
                    db.add(msg)

                student_id_counter += 1

        db.commit()
        print(f"Seeded {student_id_counter} students across {len(school_ids)} schools.")
        print(f"  - Applications created")
        print(f"  - Messages created")
        print(f"  - Saved jobs created")
        print(f"  - Jobs in DB: {len(job_ids)}")

    finally:
        db.close()


if __name__ == "__main__":
    seed()
