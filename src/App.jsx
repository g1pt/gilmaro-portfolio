import {
  ArrowRight,
  BarChart3,
  BookOpenCheck,
  CheckCircle2,
  Database,
  Download,
  ExternalLink,
  GitBranch,
  Github,
  Mail,
  MapPin,
  MonitorCheck,
  Network,
  Workflow,
} from "lucide-react";
import React, { useState } from "react";

const cvPath = "/cv/Gilmaro_Piter_CV.pdf";
const houseOfBetaCvPath = "/cv/Gilmaro_Piter_CV_HouseOfBeta.md";
const profilePath = "/profile/gilmaro-profile.jpg";
const email = "gilmaropiter@gmail.com";
const githubUrl = "https://github.com/g1pt/systematic-trading-research-engine";

const buildCards = [
  {
    icon: Database,
    title: "Data pipelines",
    text: "Data verzamelen, opschonen en structureren zodat analyse herhaalbaar wordt.",
  },
  {
    icon: MonitorCheck,
    title: "Monitoring dashboards",
    text: "Inzicht geven in resultaten, status en proceskwaliteit.",
  },
  {
    icon: BookOpenCheck,
    title: "Research platforms",
    text: "Omgevingen bouwen waarin ideeen, datasets en resultaten overzichtelijk getest worden.",
  },
  {
    icon: Workflow,
    title: "Process automation",
    text: "Handmatige stappen vervangen door workflows, logging en reproduceerbare processen.",
  },
  {
    icon: GitBranch,
    title: "Rule-based decision systems",
    text: "Beslisregels vertalen naar duidelijke, testbare logica.",
  },
  {
    icon: BarChart3,
    title: "Time-series analysis",
    text: "Werken met historische data, patronen, validatie en meetbare uitkomsten.",
  },
];

const projects = [
  {
    title: "OpenAxis",
    label: "Research & Monitoring Platform",
    description:
      "OpenAxis is een onderzoeks- en monitoringplatform voor het structureren, valideren en analyseren van datasets. De focus ligt op datakwaliteit, reproduceerbare analyses, monitoring en het inzichtelijk maken van resultaten via dashboards.",
    tech: ["Python", "FastAPI", "PostgreSQL", "Docker", "Data Pipelines"],
    impact: [
      "Minder handmatig analysewerk",
      "Centrale omgeving voor datasets en resultaten",
      "Reproduceerbare analyses",
      "Datakwaliteit en validatie",
      "Monitoring van processen en resultaten",
      "Schaalbare basis voor verdere uitbreiding",
    ],
  },
  {
    title: "Rule-Based Decision Support System",
    label: "Decision Support / Logic Engine",
    description:
      "Een systeem waarin beslisregels zijn vertaald naar gestructureerde logica, automatische logging en uitlegbare processtappen.",
    tech: ["Python", "State Machines", "Event Logging", "Testing"],
    impact: [
      "Consistente analyse",
      "Minder menselijke bias",
      "Beter testbare beslisregels",
      "Inzicht in waarom een signaal, status of uitkomst ontstaat",
    ],
  },
  {
    title: "Multi-Market Research Engine",
    label: "Data Validation / Research Environment",
    description:
      "Een validatieomgeving voor het testen, vergelijken en beoordelen van modellen over meerdere datasets.",
    tech: ["Python", "Pandas", "CSV Processing", "Walk-Forward Analysis"],
    impact: [
      "Objectieve selectie van sterke en zwakke modellen",
      "Datagedreven besluitvorming",
      "Betere kwaliteitscontrole op datasets",
    ],
  },
  {
    title: "Automation & API Integrations",
    label: "Process Automation / API Workflows",
    description:
      "Experimenten met API-koppelingen, webhooks, logging en monitoring om processen automatisch events te laten verwerken.",
    tech: ["Python", "FastAPI", "APIs", "Webhooks", "Logging"],
    impact: [
      "Minder handmatige opvolging",
      "Duidelijke eventregistratie",
      "Betere controle over systeemgedrag",
    ],
  },
];

const processSteps = [
  "Probleem begrijpen",
  "Data verzamelen",
  "Proces modelleren",
  "Automatiseren",
  "Meten",
  "Verbeteren",
];

const coreSkills = [
  "Python",
  "SQL",
  "Data Analysis",
  "Process Automation",
  "PostgreSQL",
  "Git",
  "FastAPI",
  "Docker",
  "Monitoring",
  "Dashboarding",
];

const additionalSkills = [
  "Data Pipelines",
  "API Integrations",
  "Webhooks",
  "Testing",
  "Research Methodology",
  "CSV Processing",
  "Time-Series Analysis",
];

const focusPoints = [
  "Analytisch vermogen",
  "Sterke leerhouding",
  "Praktische projectervaring",
  "Eigenaarschap",
  "Discipline vanuit topsport",
  "Procesmatig denken",
  "Beschikbaar voor 32+ uur",
  "Open voor begeleiding en groei",
];

function CvButton({
  variant = "primary",
  href = cvPath,
  label = "Download CV",
}) {
  return (
    <a
      className={`button button-${variant}`}
      href={href}
      target="_blank"
      rel="noreferrer"
      download
    >
      <Download size={18} aria-hidden="true" />
      {label}
    </a>
  );
}

function ProfilePhoto() {
  const [imageAvailable, setImageAvailable] = useState(true);

  return (
    <div className="profile-photo-card" aria-label="Profielfoto Gilmaro Piter">
      {imageAvailable ? (
        <img
          src={profilePath}
          alt="Gilmaro Piter"
          onError={() => setImageAvailable(false)}
        />
      ) : (
        <span>GP</span>
      )}
    </div>
  );
}

function SectionHeader({ eyebrow, title, text }) {
  return (
    <div className="section-header">
      <p className="eyebrow">{eyebrow}</p>
      <h2>{title}</h2>
      {text ? <p>{text}</p> : null}
    </div>
  );
}

function App() {
  return (
    <main>
      <header className="site-header">
        <a className="brand" href="#top" aria-label="Gilmaro Piter home">
          <span>GP</span>
          <strong>Gilmaro Piter</strong>
        </a>
        <nav aria-label="Hoofdnavigatie">
          <a href="#projects">Projecten</a>
          <a href="#skills">Skills</a>
          <a href="#house-of-beta">House of Beta</a>
          <a href="#contact">Contact</a>
        </nav>
      </header>

      <section className="hero" id="top">
        <div className="hero-content">
          <p className="eyebrow">Junior Data & Systems Specialist</p>
          <h1>Data • Automation • Research Systems</h1>
          <p className="hero-subtitle">
            Ik bouw systemen die complexe processen inzichtelijk, meetbaar en
            automatiseerbaar maken.
          </p>
          <p className="hero-text">
            Met een achtergrond in topsport en jarenlange zelfstudie heb ik
            gewerkt aan projecten rondom data-analyse, automatisering,
            monitoring en onderzoeksplatformen. De focus ligt op gestructureerd
            werken, data begrijpen en processen verbeteren.
          </p>
          <div className="badge-row" aria-label="Kerninformatie">
            <span>Beschikbaar 32+ uur</span>
            <span>Regio Rotterdam</span>
            <span>Open voor traineeships</span>
            <span>Data & Automation</span>
          </div>
          <div className="hero-actions">
            <a className="button button-primary" href="#projects">
              Bekijk projecten
              <ArrowRight size={18} aria-hidden="true" />
            </a>
            <CvButton variant="secondary" />
            <a className="button button-ghost" href={`mailto:${email}`}>
              <Mail size={18} aria-hidden="true" />
              Neem contact op
            </a>
          </div>
        </div>

        <aside className="hero-panel" aria-label="Profiel samenvatting">
          <ProfilePhoto />
          <div className="signal-card">
            <span className="signal-dot" />
            <p>Richting</p>
            <strong>Junior Data & Systems Specialist</strong>
          </div>
          <div className="metric-grid">
            <div>
              <span>Focus</span>
              <strong>Data, processen, dashboards</strong>
            </div>
            <div>
              <span>Basis</span>
              <strong>Python, SQL, API's, Docker</strong>
            </div>
            <div>
              <span>Werkstijl</span>
              <strong>Zelfstandig, meetbaar, leerbaar</strong>
            </div>
            <div>
              <span>Beschikbaar</span>
              <strong>Vanaf 32 uur per week</strong>
            </div>
          </div>
        </aside>
      </section>

      <section className="section about" id="about">
        <SectionHeader
          eyebrow="Over mij"
          title="Niet traditioneel, wel praktisch opgebouwd"
        />
        <div className="about-grid">
          <div className="copy-stack">
            <p>
              Mijn achtergrond ligt niet in een traditionele IT-opleiding. Tien
              jaar topsport heeft gezorgd voor discipline, doorzettingsvermogen,
              samenwerking en een sterke focus op continue verbetering.
            </p>
            <p>
              Die mentaliteit pas ik nu toe op data, software en automatisering.
              De afgelopen jaren heb ik zelfstandig gewerkt aan projecten waarin
              data, processen en technische oplossingen samenkomen.
            </p>
            <p>
              Mijn interesse begon binnen financiële markten, maar de kern van
              mijn werk ligt breder: data structureren, processen modelleren,
              systemen bouwen en resultaten meetbaar maken.
            </p>
            <p>
              Ik zoek een omgeving waarin ik verder kan groeien binnen data,
              automatisering, applicatiebeheer en research systems.
            </p>
          </div>
          <div className="profile-card">
            <Network size={28} aria-hidden="true" />
            <h3>Research Systems Builder</h3>
            <p>
              Projecten gebouwd rond datastromen, validatie, logging,
              dashboards en herhaalbare analyses.
            </p>
            <ul>
              <li>Zelfstudie richting IT, data en automatisering</li>
              <li>Ervaring met lesgeven en samenwerken</li>
              <li>Open voor junior rollen, traineeships en begeleiding</li>
            </ul>
          </div>
        </div>
      </section>

      <section className="section" id="build">
        <SectionHeader
          eyebrow="Wat ik bouw"
          title="Praktische systemen voor data en besluitvorming"
          text="De nadruk ligt op herhaalbare processen, duidelijke logging en inzicht in wat een systeem doet."
        />
        <div className="card-grid">
          {buildCards.map(({ icon: Icon, title, text }) => (
            <article className="feature-card" key={title}>
              <Icon size={26} aria-hidden="true" />
              <h3>{title}</h3>
              <p>{text}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="section projects" id="projects">
        <SectionHeader
          eyebrow="Projecten"
          title="Gebouwd vanuit onderzoek, automatisering en meetbaarheid"
          text="De technische waarde zit in data, validatie, workflow, logging, dashboards en systeemontwerp."
        />
        <div className="project-list">
          {projects.map((project) => (
            <article className="project-card" key={project.title}>
              <div className="project-main">
                <span className="project-label">{project.label}</span>
                <h3>{project.title}</h3>
                <p>{project.description}</p>
                <div className="tech-row">
                  {project.tech.map((tech) => (
                    <span key={tech}>{tech}</span>
                  ))}
                </div>
              </div>
              <div className="impact-box">
                <strong>Impact</strong>
                <ul>
                  {project.impact.map((item) => (
                    <li key={item}>
                      <CheckCircle2 size={16} aria-hidden="true" />
                      {item}
                    </li>
                  ))}
                </ul>
              </div>
            </article>
          ))}
        </div>
      </section>

      <section className="section process">
        <SectionHeader
          eyebrow="Werkwijze"
          title="Van probleem naar meetbare verbetering"
          text="Ik werk graag vanuit een duidelijk probleem. Eerst begrijpen wat er gebeurt, daarna data structureren, regels of processen modelleren en vervolgens meten of de oplossing waarde toevoegt."
        />
        <div className="timeline">
          {processSteps.map((step, index) => (
            <div className="timeline-step" key={step}>
              <span>{String(index + 1).padStart(2, "0")}</span>
              <strong>{step}</strong>
            </div>
          ))}
        </div>
      </section>

      <section className="section" id="skills">
        <SectionHeader
          eyebrow="Skills"
          title="Kernvaardigheden"
          text="Een praktische technische basis voor data, automatisering, monitoring en procesverbetering."
        />
        <div className="skills-layout">
          <div>
            <h3>Kernvaardigheden</h3>
            <div className="skill-cloud">
              {coreSkills.map((skill) => (
                <span key={skill}>{skill}</span>
              ))}
            </div>
          </div>
          <div>
            <h3>Aanvullend</h3>
            <div className="skill-cloud">
              {additionalSkills.map((skill) => (
                <span key={skill}>{skill}</span>
              ))}
            </div>
          </div>
        </div>
      </section>

      <section className="section recruiter">
        <div>
          <p className="eyebrow">Recruiter Samenvatting</p>
          <h2>Junior Data & Systems Specialist</h2>
          <p>
            Junior Data & Systems Specialist met een achtergrond in topsport,
            onderwijs en jarenlange zelfstudie richting IT. Praktisch ingesteld,
            analytisch en gewend om zelfstandig complexe onderwerpen eigen te
            maken. Ervaring met Python, SQL, FastAPI, PostgreSQL, Docker,
            monitoring, dashboards, API-integraties en procesautomatisering.
            Sterk in discipline, eigenaarschap, samenwerken en continu
            verbeteren. Op zoek naar een traineeship of junior functie waarin
            data, technologie en procesverbetering samenkomen.
          </p>
        </div>
        <div className="focus-grid">
          {focusPoints.map((point) => (
            <span key={point}>
              <CheckCircle2 size={17} aria-hidden="true" />
              {point}
            </span>
          ))}
        </div>
      </section>

      <section className="section house-beta" id="house-of-beta">
        <div>
          <p className="eyebrow">Waarom House of Bèta</p>
          <h2>Analytisch leren in echte projecten</h2>
          <p>
            Ik ben op zoek naar een omgeving waarin analytisch denken, leren in
            de praktijk en het oplossen van complexe vraagstukken centraal
            staan.
          </p>
          <p>
            De combinatie van data, technologie en persoonlijke ontwikkeling
            sluit goed aan bij hoe ik mezelf de afgelopen jaren heb ontwikkeld.
          </p>
          <p>
            Mijn achtergrond is niet traditioneel, maar juist daardoor heb ik
            geleerd om zelfstandig kennis op te bouwen, feedback te verwerken en
            nieuwe onderwerpen stap voor stap eigen te maken.
          </p>
          <p>
            House of Bèta spreekt mij aan omdat het ruimte biedt om te groeien,
            praktijkervaring op te doen en tegelijkertijd waarde toe te voegen
            aan echte projecten.
          </p>
        </div>
        <div className="house-beta-actions">
          <CvButton
            variant="primary"
            href={houseOfBetaCvPath}
            label="Download CV (House of Beta)"
          />
          <a className="button button-ghost" href="#contact">
            <Mail size={18} aria-hidden="true" />
            Neem contact op
          </a>
        </div>
      </section>

      <section className="section contact" id="contact">
        <SectionHeader
          eyebrow="Contact"
          title="Open voor junior rollen, traineeships en projecten"
          text="Interesse in data, automatisering, applicatiebeheer en research systems."
        />
        <div className="contact-card">
          <div className="contact-details">
            <p>
              <strong>Naam</strong>
              <span>Gilmaro Piter</span>
            </p>
            <p>
              <strong>Email</strong>
              <a href={`mailto:${email}`}>{email}</a>
            </p>
            <p>
              <strong>Regio</strong>
              <span>Rotterdam / Nederland</span>
            </p>
            <p>
              <strong>LinkedIn</strong>
              <span>Placeholder</span>
            </p>
            <p>
              <strong>GitHub</strong>
              <a href={githubUrl} target="_blank" rel="noreferrer">
                GitHub projectrepository
              </a>
            </p>
          </div>
          <div className="contact-actions">
            <a className="button button-primary" href={`mailto:${email}`}>
              <Mail size={18} aria-hidden="true" />
              Mail mij
            </a>
            <a
              className="button button-secondary"
              href={githubUrl}
              target="_blank"
              rel="noreferrer"
            >
              <Github size={18} aria-hidden="true" />
              Bekijk GitHub
              <ExternalLink size={15} aria-hidden="true" />
            </a>
            <CvButton variant="ghost" />
            <CvButton
              variant="ghost"
              href={houseOfBetaCvPath}
              label="Download CV (House of Beta)"
            />
          </div>
        </div>
      </section>

      <footer>
        <span>Gilmaro Piter</span>
        <span>
          <MapPin size={15} aria-hidden="true" />
          Rotterdam / Nederland
        </span>
      </footer>
    </main>
  );
}

export default App;
