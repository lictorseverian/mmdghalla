// dump <seed_name> <genver> <west_x> <north_z> <nx> <ny> <m_per_px> <out_prefix>
// writes <out>.biome (u16 LE) <out>.height (f32 LE) <out>.forest (f32 LE); rows north->south
use std::io::Write;
use worldgen::geo::WorldGenerator;
fn main() {
    let a: Vec<String> = std::env::args().collect();
    let wg = WorldGenerator::from_seed_name(&a[1], a[2].parse().unwrap());
    eprintln!("seed={}", wg.seed);
    let (ox, oz): (f32, f32) = (a[3].parse().unwrap(), a[4].parse().unwrap());
    let (nx, ny): (usize, usize) = (a[5].parse().unwrap(), a[6].parse().unwrap());
    let mpp: f32 = a[7].parse().unwrap();
    let threads = std::thread::available_parallelism().map(|n| n.get()).unwrap_or(4);
    let wg = std::sync::Arc::new(wg);
    let rows_per = (ny + threads - 1) / threads;
    let hs: Vec<_> = (0..threads).map(|t| {
        let wg = wg.clone();
        std::thread::spawn(move || {
            let (j0, j1) = (t * rows_per, ((t + 1) * rows_per).min(ny));
            let mut b = Vec::new(); let mut h = Vec::new(); let mut f = Vec::new();
            for j in j0..j1 {
                let wz = oz - (j as f32 + 0.5) * mpp;
                for i in 0..nx {
                    let wx = ox + (i as f32 + 0.5) * mpp;
                    let (bi, he) = wg.sample(wx, wz);
                    b.push(bi as u16); h.push(he); f.push(wg.forest_factor(wx, wz));
                }
            }
            (b, h, f)
        })
    }).collect();
    let p = &a[8];
    let mut fb = std::fs::File::create(format!("{p}.biome")).unwrap();
    let mut fh = std::fs::File::create(format!("{p}.height")).unwrap();
    let mut ff = std::fs::File::create(format!("{p}.forest")).unwrap();
    for h in hs { let (b, he, f) = h.join().unwrap();
        for v in b { fb.write_all(&v.to_le_bytes()).unwrap(); }
        for v in he { fh.write_all(&v.to_le_bytes()).unwrap(); }
        for v in f { ff.write_all(&v.to_le_bytes()).unwrap(); } }
}
