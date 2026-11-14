const session = [0, 40, 25, 80, 60, 140, 110, 190, 160, 240, 210, 280];

const orders = [
  { time: "09:41", side: "Buy", qty: 1, price: "5,842.25", state: "Filled" },
  { time: "10:16", side: "Sell", qty: 1, price: "5,846.00", state: "Filled" },
  { time: "11:02", side: "Buy", qty: 1, price: "5,839.50", state: "Filled" },
  { time: "13:28", side: "Sell", qty: 1, price: "5,844.75", state: "Working" },
];

function sparkline(values) {
  const width = 560;
  const height = 140;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const points = values
    .map((value, index) => {
      const x = (index / (values.length - 1)) * width;
      const y = height - ((value - min) / (max - min)) * (height - 16) - 8;
      return `${x},${y}`;
    })
    .join(" ");
  return { width, height, points };
}

export default function App() {
  const line = sparkline(session);

  return (
    <main className="desk">
      <p className="banner">Sample data. Not connected to a broker.</p>
      <header className="top">
        <div>
          <p className="kicker">Paper desk</p>
          <h1>MES · sample session</h1>
        </div>
        <div className="switch">
          <span>Kill switch</span>
          <strong>Armed</strong>
        </div>
      </header>

      <section className="stats">
        <article>
          <span>Day P&L</span>
          <strong className="up">+$280</strong>
        </article>
        <article>
          <span>Position</span>
          <strong>Short 1</strong>
        </article>
        <article>
          <span>Trades today</span>
          <strong>6 / 10</strong>
        </article>
        <article>
          <span>Daily loss used</span>
          <strong>$0 / $200</strong>
        </article>
      </section>

      <section className="grid">
        <article className="panel">
          <h2>Session equity</h2>
          <svg viewBox={`0 0 ${line.width} ${line.height}`} role="img" aria-label="Sample session equity">
            <polyline points={line.points} />
          </svg>
        </article>
        <article className="panel">
          <h2>Last signal</h2>
          <p className="signal">Flat</p>
          <p className="note">Dummy example strategy. No private rules are loaded.</p>
        </article>
      </section>

      <section className="panel">
        <h2>Recent orders</h2>
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Side</th>
              <th>Qty</th>
              <th>Price</th>
              <th>State</th>
            </tr>
          </thead>
          <tbody>
            {orders.map((order) => (
              <tr key={`${order.time}-${order.side}`}>
                <td>{order.time}</td>
                <td>{order.side}</td>
                <td>{order.qty}</td>
                <td>{order.price}</td>
                <td>{order.state}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </main>
  );
}
