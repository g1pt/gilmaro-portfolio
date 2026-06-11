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
import React, { useEffect, useState } from "react";

const cvPath = "/cv/Gilmaro_Piter_CV.pdf";
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
    text: "Omgevingen bouwen waarin ideeën, datasets en resultaten overzichtelijk getest worden.",
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
      "Een platform voor het structureren van historische datasets, backtests en dashboards.",
    tech: ["Python", "FastAPI", "PostgreSQL", "Docker", "Data Pipelines"],
    impact: [
      "Minder handmatig analysewerk",
      "Centrale plek voor data en resultaten",
      "Herhaalbare onderzoeksprocessen",
      "Schaalbare basis voor verdere uitbreiding",
    ],
  },
  {
    title: "Rule-Based Decision Support System",
    label: "Automation / Logic Engine",
    description:
      "Een systeem waarin subjectieve beslisregels zijn vertaald naar gestructureerde logica en automatische logging.",
    tech: ["Python", "State Machines", "Event Logging", "Testing"],
    impact: [
      "Consistente analyse",
      "Minder menselijke bias",
      "Beter testbare beslisregels",
      "Inzicht in waarom een signaal wel of niet ontstaat",
    ],
  },
  {
    title: "Multi-Market Research Engine",
    label: "Data Validation & Research",
    description:
      "Een onderzoeksomgeving voor het testen, vergelijken en valideren van modellen over meerdere datasets.",
    tech: ["Python", "Pandas", "CSV Processing", "Walk-Forward Analysis"],
    impact: [
      "Objectieve selectie van sterke en zwakke modellen",
      "Datagedreven besluitvorming",
      "Betere kwaliteitscontrole op datasets",
    ],
  },
  {
    title: "API & Automation Integrations",
    label: "Workflow Automation",
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

const skills = [
  "Python",
  "SQL",
  "FastAPI",
  "PostgreSQL",
  "Docker",
  "Git",
  "Data Analysis",
  "Data Pipelines",
  "Dashboarding",
  "Process Automation",
  "Monitoring",
  "Research Methodology",
  "CSV Processing",
  "Testing",
  "API Integrations",
  "Webhooks",
  "Time-Series Analysis",
];

const focusPoints = [
  "Sterke leerhouding",
  "Praktische projectervaring",
  "Discipline vanuit topsport",
  "Interesse in data en processen",
  "Beschikbaar voor 32+ uur",
  "Open voor begeleiding en verdere ontwikkeling",
];

function CvButton({ variant = "primary", cvAvailable }) {
  if (!cvAvailable) {
    return (
      <span className={`button button-${variant} button-disabled`}>
        <Download size={18} aria-hidden="true" />
        CV beschikbaar op aanvraag
      </span>
    );
  }

  return (
    <a className={`button button-${variant}`} href={cvPath} download>
      <Download size={18} aria-hidden="true" />
      Download CV
    </a>
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
  const [cvAvailable, setCvAvailable] = useState(false);

  useEffect(() => {
    let cancelled = false;

    fetch(cvPath, { cache: "no-store" })
      .then((response) => {
        const contentType = response.headers.get("content-type") || "";
        if (!cancelled) {
          setCvAvailable(response.ok && contentType.includes("application/pdf"));
        }
      })
      .catch(() => {
        if (!cancelled) {
          setCvAvailable(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, []);

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
          <a href="#contact">Contact</a>
        </nav>
      </header>

      <section className="hero" id="top">
        <div className="hero-content">
          <p className="eyebrow">Self-Taught Data & Automation Engineer</p>
          <h1>Data, Automation & Research Systems</h1>
          <p className="hero-subtitle">
            Ik bouw systemen die complexe processen inzichtelijk, meetbaar en
            automatiseerbaar maken.
          </p>
          <p className="hero-text">
            Met een achtergrond in topsport en jarenlange zelfstudie heb ik
            gewerkt aan Python-projecten rondom data-analyse, automatisering,
            monitoring en research platforms. Mijn focus ligt op gestructureerd
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
            <CvButton variant="secondary" cvAvailable={cvAvailable} />
            <a className="button button-ghost" href={`mailto:${email}`}>
              <Mail size={18} aria-hidden="true" />
              Neem contact op
            </a>
          </div>
        </div>

        <aside className="hero-panel" aria-label="Profiel samenvatting">
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
        <SectionHeader eyebrow="Over mij" title="Niet traditioneel, wel praktisch opgebouwd" />
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
              Ik zoek een omgeving waarin ik verder kan groeien binnen IT, data,
              analyse en applicatiebeheer.
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
          text="Trading komt alleen terug als domeincontext; de technische waarde zit in data, validatie, workflow en systeemontwerp."
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
          title="Technische basis"
          text="Een praktische stack voor junior data-, automation- en systemsrollen."
        />
        <div className="skill-cloud">
          {skills.map((skill) => (
            <span key={skill}>{skill}</span>
          ))}
        </div>
      </section>

      <section className="section recruiter">
        <div>
          <p className="eyebrow">Recruiter focus</p>
          <h2>Waarom dit profiel past bij een traineeship</h2>
          <p>
            Mijn profiel is niet traditioneel, maar wel praktisch en leergierig.
            Door topsport, zelfstudie en eigen projecten heb ik geleerd om
            zelfstandig te werken, feedback te verwerken en stap voor stap
            complexe onderwerpen eigen te maken.
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
                g1pt/systematic-trading-research-engine
              </a>
            </p>
          </div>
          <div className="contact-actions">
            <a className="button button-primary" href={`mailto:${email}`}>
              <Mail size={18} aria-hidden="true" />
              Mail mij
            </a>
            <a className="button button-secondary" href={githubUrl} target="_blank" rel="noreferrer">
              <Github size={18} aria-hidden="true" />
              Bekijk GitHub
              <ExternalLink size={15} aria-hidden="true" />
            </a>
            <CvButton variant="ghost" cvAvailable={cvAvailable} />
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
